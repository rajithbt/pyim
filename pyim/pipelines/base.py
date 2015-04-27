from __future__ import (absolute_import, division,
                        print_function, unicode_literals)
from builtins import (ascii, bytes, chr, dict, filter, hex, input,
                      int, map, next, oct, open, pow, range, round,
                      str, super, zip)
from future.utils import native_str

import logging
from contextlib import contextmanager
from collections import defaultdict
from enum import Enum
from multiprocessing import Pool

import pandas as pd
import numpy as np
from scipy.spatial.distance import pdist
from tkgeno.io import FastqFile, FastaFile

from pyim.util import PrioritySet

logging.basicConfig(
    format='%(asctime)-15s %(message)s',
    datefmt='[%Y-%m-%d %H:%M:%S]',
    level=logging.INFO)


class Pipeline(object):

    def __init__(self, extractor, aligner, identifier):
        super().__init__()
        self._extractor = extractor
        self._aligner = aligner
        self._identifier = identifier

    @classmethod
    def configure_argparser(cls, parser):
        raise NotImplementedError()

    @classmethod
    def from_args(cls, args):
        raise NotImplementedError()

    def run(self, input_path, output_path):
        logger = logging.getLogger()

        logger.info('Starting {} pipeline'.format(
            self.__class__.__name__.replace('Pipeline', '')))

        # Create directories if needed.
        if not output_path.exists():
            output_path.mkdir()

        if input_path.suffix not in {'.bam', '.sam'}:
            genomic_path = output_path / ('genomic' + input_path.suffix)
            barcode_path = output_path / 'genomic.barcodes.txt'

            # Extract genomic reads from input.
            logger.info('Extracting genomic sequences from reads')

            _, barcodes = self._extractor.extract_file(
                input_path=input_path, output_path=genomic_path)

            # Log statistics.
            total_reads = sum(self._extractor.stats.values())

            logger.info('- Processed {} reads'.format(total_reads))
            logger.info('- Read statistics')
            for status in self._extractor.STATUS:
                count = self._extractor.stats[status]
                logger.info('\t- {}: {} ({:3.2f}%)'
                            .format(status.name, count,
                                    (count / total_reads) * 100))

            # Write out barcodes as frame.
            barcode_frame = pd.DataFrame.from_records(
                iter(barcodes.items()), columns=['read_id', 'barcode'])
            barcode_frame.to_csv(
                str(barcode_path), sep=native_str('\t'), index=False)

            # Align to reference genome.
            logger.info('Aligning genomic sequences to reference')
            logger.info('- Using {} aligner (v{})'.format(
                self._aligner.__class__.__name__.replace('Aligner', ''),
                self._aligner.get_version()))

            aln_path = self._aligner.align_file(
                file=genomic_path, output_dir=output_path)
        else:
            aln_path, barcodes = input_path, None

        # Identify transposon insertions.
        logger.info('Identifying insertions from alignment')

        insertions = self._identifier.identify(aln_path, sample_map=barcodes)
        insertions.to_csv(str(output_path / 'insertions.txt'),
                          sep=native_str('\t'), index=False)

        logger.info('Done!')


class GenomicExtractor(object):

    def __init__(self, min_length=1, threads=1, chunk_size=1000, **kwargs):
        super().__init__()
        self._min_length = min_length
        self._threads = threads
        self._chunk_size = chunk_size
        self._stats = defaultdict(int)

    @property
    def stats(self):
        return self._stats

    def extract(self, reads):
        if self._threads == 1:
            for read in reads:
                result, status = self.extract_read(read)
                self._stats[status] += 1
                if result is not None:
                    yield result
        else:
            pool = Pool(self._threads)

            for result, status in pool.imap_unordered(
                    self.extract_read, reads, chunksize=self._chunk_size):
                self._stats[status] += 1
                if result is not None:
                    yield result

            pool.close()
            pool.join()

    def extract_read(self, read):
        raise NotImplementedError()

    @classmethod
    def _read_input(cls, file_path, format_):
        raise NotImplementedError()

    @classmethod
    @contextmanager
    def _open_out(cls, file_path, format_):
        raise NotImplementedError()

    @classmethod
    def _write_out(cls, sequence, fh, format_):
        raise NotImplementedError()

    def extract_from_file(self, file_path, format_=None):
        reads = self._read_input(file_path, format_=format_)
        for genomic, barcode in self.extract(reads):
                yield genomic, barcode

    def extract_to_file(self, reads, file_path, format_=None):
        barcodes = {}

        with self._open_out(file_path, format_=format_) as file_:
            for genomic, barcode in self.extract(reads):
                barcodes[genomic.id] = barcode
                self._write_out(genomic, file_, format_=format_)

        return file_path, barcodes

    def extract_file(self, input_path, output_path,
                     in_format=None, out_format=None):
        reads = self._read_input(input_path, format_=in_format)
        return self.extract_to_file(reads, output_path, format_=out_format)


class InsertionIdentifier(object):

    def __init__(self, **kwargs):
        super().__init__()

    def identify(self, alignment):
        raise NotImplementedError()

    @classmethod
    def _group_alignments_by_position(cls, alignments, barcode_map=None):
        grouped = cls._group_alignments_by_pos(alignments)

        if barcode_map is None:
            for tup, grp in grouped:
                yield tup + (np.nan, ), grp
        else:
            for tup, grp in grouped:
                for bc, bc_grp in cls._split_by_barcode(grp, barcode_map):
                    yield tup + (bc, ), bc_grp

    @staticmethod
    def _group_alignments_by_pos(alignments):
        """ Groups alignments by their positions, grouping forward strand
            alignments with the same start position and reverse strand
            alignments with the same end position. Assumes alignments
            are all on a single reference sequence.
        """
        # Setup our collections for tracking reads and positions.
        #
        # The priority set is used to track positions with alignment groups,
        # ensuring that no position is listed twice (the set part) and
        # always giving the lowest position first (the priority part).
        #
        # The alignment dict contains two lists for each position with at
        # least one alignment, one for forward reads and one for reverse.
        # Any alignments encountered as position x in orientation o are added
        # to the corresponding entry dict[x][o] in the list, in which
        # o is encoded as {0,1}, with 1 being for reverse strand alignments.
        position_set = PrioritySet()
        aln_dict = defaultdict(lambda: ([], []))

        curr_pos = 0
        for aln in alignments:
            # Check our ordering.
            if aln.reference_start < curr_pos:
                raise ValueError('Alignments not ordered by position')

            curr_pos = aln.reference_start

            # Add current read to collections.
            is_reverse = aln.is_reverse
            ref_pos = aln.reference_end if is_reverse else curr_pos
            aln_dict[ref_pos][bool(is_reverse)].append(aln)
            position_set.push(ref_pos, ref_pos)

            # Return any alignment groups before our current position.
            try:
                while position_set.first() < curr_pos:
                    first_pos = position_set.pop()
                    fwd_grp, rev_grp = aln_dict.pop(first_pos)
                    if len(fwd_grp) > 0:
                        yield (fwd_grp[0].reference_start, 1), fwd_grp
                    if len(rev_grp) > 0:
                        yield (rev_grp[0].reference_end, -1), rev_grp
            except ValueError:
                pass

        # We're done, yield any remaining alignment groups.
        for _ in range(len(position_set)):
            fwd_grp, rev_grp = aln_dict.pop(position_set.pop())
            if len(fwd_grp) > 0:
                yield (fwd_grp[0].reference_start, 1), fwd_grp
            if len(rev_grp) > 0:
                yield (rev_grp[0].reference_end, -1), rev_grp

    @staticmethod
    def _split_by_barcode(alignments, barcode_map):
        split_groups = defaultdict(list)
        for aln in alignments:
            barcode = barcode_map[aln.query_name]
            split_groups[barcode].append(aln)

        for k, v in split_groups.items():
            yield k, v


def genomic_distance(insertions):
    loc = insertions['location']
    loc_2d = np.vstack([loc, np.zeros_like(loc)]).T
    dist = pdist(loc_2d, lambda u, v: np.abs(u-v).sum())
    return dist
