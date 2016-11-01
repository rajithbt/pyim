import contextlib
import itertools
import os
import subprocess

import numpy as np
import pandas as pd
import pysam
import toolz


def _parse_float(value):
    try:
        return float(value)
    except ValueError:
        return np.nan


def _reorder_columns(frame, order):
    columns = list(order)
    extra_columns = sorted([c for c in frame.columns if c not in set(columns)])
    return frame[columns + extra_columns]


def _get_region(frame,
                reference,
                start=None,
                end=None,
                filters=None,
                incl_left=True,
                incl_right=True,
                ref_col='contig',
                start_col='start',
                end_col='end'):

    mask = pd.Series(True, index=frame.index)

    # Filter on passed range.
    if reference is not None:
        mask = frame[ref_col] == reference

    if start is not None:
        mask &= frame[start_col] <= end

    if end is not None:
        mask &= frame[end_col] >= start

    # Filter for inclusiveness.
    if not incl_left:
        mask &= frame['start'] > start

    if not incl_right:
        mask &= frame['end'] < end

    # Apply any additional filters.
    if filters is not None:
        for name, value in filters.items():
            mask &= frame[name] == value

    return frame.ix[mask]


def bgzip(file_path, out_path=None):
    if out_path is None:
        out_path = file_path + '.gz'

    with open(out_path, 'w') as out_file:
        subprocess.check_call(['bgzip', '-c', str(file_path)], stdout=out_file)

    return out_path


def tabix(file_path, preset):
    subprocess.check_call(['tabix', '-p', preset, file_path])


class TabixIterator(object):
    def __init__(self, file_path, parser=None):
        self._file_path = file_path
        self._parser = parser

    def fetch(self,
              reference=None,
              start=None,
              end=None,
              filters=None,
              incl_left=True,
              incl_right=True):
        file_obj = pysam.TabixFile(str(self._file_path), parser=self._parser)

        with contextlib.closing(file_obj) as tb_file:
            if reference is not None:
                reference = str(reference)

            records = self._fetch(
                tb_file, reference=reference, start=start, end=end)

            # Filter records on additional filters.
            if filters is not None:
                for name, value in filters.items():
                    records = self._apply_filter(records, name, value)

            # Filter inclusive/exclusive if needed.
            if not incl_left:
                records = (r for r in records if r.start > start)

            if not incl_right:
                records = (r for r in records if r.end < end)

            # Yield records.
            for record in records:
                yield record

    @staticmethod
    def _apply_filter(records, name, value):
        return (rec for rec in records
                if hasattr(rec, name) and getattr(rec, name) == value)

    def _fetch(self, tb_file, reference=None, **kwargs):
        # For some reason pysam does not fetch all records if reference
        # is None under Python 2.7. To fix this, here we simply chain all
        # the contig records into one iterable.
        if reference is None:
            contigs = tb_file.contigs
            records = itertools.chain.from_iterable((tb_file.fetch(
                reference=ref, **kwargs) for ref in contigs))
        else:
            records = tb_file.fetch(reference=reference, **kwargs)

        for record in records:
            yield record


class TabixFile(object):
    def __init__(self, file_path, parser):
        self._file_path = file_path
        self._iterator = TabixIterator(file_path, parser=parser)

    def fetch(self,
              reference=None,
              start=None,
              end=None,
              filters=None,
              incl_left=True,
              incl_right=True):
        """Fetches records for the given region."""
        records = self._iterator.fetch(
            reference=reference,
            start=start,
            end=end,
            filters=filters,
            incl_left=incl_left,
            incl_right=incl_right)

        for record in records:
            yield self._to_dict(record)

    def get_region(self,
                   reference=None,
                   start=None,
                   end=None,
                   filters=None,
                   incl_left=True,
                   incl_right=True):
        """Fetches DataFrame of features for the given region."""
        records = self.fetch(
            reference,
            start,
            end,
            filters=filters,
            incl_left=incl_left,
            incl_right=incl_right)
        return self._frame_constructor().from_records(records)

    def _to_dict(self, record):
        raise NotImplementedError()

    @classmethod
    def _frame_constructor(cls):
        raise NotImplementedError()


class TabixFrame(pd.DataFrame):
    @property
    def _constructor(self):
        raise NotImplementedError()

    def fetch(self,
              reference=None,
              start=None,
              end=None,
              filters=None,
              incl_left=True,
              incl_right=True):

        subset = self.get_region(
            reference,
            start,
            end,
            filters=filters,
            incl_left=incl_left,
            incl_right=incl_right)

        for tup in subset.itertuples():
            yield tup._asdict()

    def get_region(self,
                   reference=None,
                   start=None,
                   end=None,
                   filters=None,
                   incl_left=True,
                   incl_right=True,
                   **kwargs):
        return _get_region(
            self,
            reference,
            start,
            end,
            filters=filters,
            incl_left=incl_left,
            incl_right=incl_right,
            **kwargs)


class GtfFile(TabixFile):

    FIELDS = ('contig', 'source', 'feature', 'start', 'end', 'score', 'strand',
              'frame')
    TYPES = {'start': int, 'end': int, 'score': _parse_float}

    def __init__(self, file_path):
        file_path = str(file_path)

        if not os.path.exists(file_path):
            raise IOError('File does not exist ({})'.format(file_path))

        if not file_path.endswith('.gz'):
            if os.path.exists(file_path + '.gz'):
                file_path += '.gz'
            else:
                file_path = self.compress(file_path)

        super().__init__(file_path, parser=pysam.asGTF())

    def _to_dict(self, record):
        basic_attr = dict(zip(self.FIELDS, record))

        for key, func in self.TYPES.items():
            basic_attr[key] = func(basic_attr[key])

        return toolz.merge(basic_attr, dict(record))

    @classmethod
    def _frame_constructor(cls):
        return GtfFrame

    def get_gene(self,
                 gene_id,
                 feature_type='gene',
                 field_name='gene_id',
                 **kwargs):
        """Fetchs a given gene by id."""

        # Add feature filter to filters (if given).
        filters = kwargs.pop('filter', {})
        filters['feature'] = feature_type

        # Search for gene record.
        records = self._iterator.fetch(filters=filters, **kwargs)
        for record in records:
            if record[str(field_name)] == gene_id:
                return self._to_series(record)

        raise ValueError('Gene {} does not exist'.format(gene_id))

    @classmethod
    def compress(cls, file_path, out_path=None, sort=True, create_index=True):
        """Compresses and indexes a gtf file using bgzip and tabix."""

        # Base output path on original file name.
        out_path = out_path or file_path + '.gz'

        if sort:
            # Sort file before compressing and indexing.
            file_path = cls.sort(file_path, out_path=out_path + '.tmp')

        # Gzip and index file.
        gzipped_path = bgzip(file_path, out_path=out_path)

        if create_index:
            tabix(gzipped_path, preset='gff')

        # Clean up temp file.
        if file_path.endswith('.tmp'):
            os.unlink(file_path)

        return gzipped_path

    @classmethod
    def sort(cls, file_path, out_path):
        """Sorts a gtf file by position, required for indexing by tabix."""
        with open(out_path, 'w') as out_file:
            cmd = '(grep ^"#" {0}; grep -v ^"#" {0} | sort -k1,1 -k4,4n)'
            subprocess.check_call(
                cmd.format(file_path), stdout=out_file, shell=True)
        return out_path

    def __repr__(self):
        return '<GtfFile file_path={!r}>'.format(self._file_path)


class GtfFrame(TabixFrame):
    @property
    def _constructor(self):
        return GtfFrame

    @classmethod
    def read_csv(cls, path, *args, **kwargs):
        frame = pd.read_csv(path, *args, sep='\t', comment='#', **kwargs)
        return cls._format_frame(frame)

    @classmethod
    def from_records(cls, data, *args, **kwargs):
        # Build frame.
        frame = super().from_records(data, *args, **kwargs)

        # Handle empty case.
        if len(frame) == 0:
            frame = cls([], columns=GtfFile.FIELDS[:-1])

        return cls._format_frame(frame)

    @classmethod
    def _format_frame(cls, frame):
        # Convert some columns to categorical.
        frame['contig'] = frame['contig'].astype('category')
        frame['feature'] = frame['feature'].astype('category')
        frame['source'] = frame['source'].astype('category')
        frame['frame'] = frame['frame'].astype('category')

        # Order columns to start with normal attributes.
        frame = _reorder_columns(frame, GtfFile.FIELDS[:-1])

        return frame

    def get_gene(self, gene_id):
        result = self.ix[((self['feature'] == 'gene') &
                          (self['gene_id'] == gene_id))]

        if len(result) == 0:
            raise ValueError('Gene {} does not exist'.format(gene_id))

        return result