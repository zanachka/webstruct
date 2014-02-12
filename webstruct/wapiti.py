# -*- coding: utf-8 -*-
"""
This module provides some utils for easier creation of Wapiti_ templates
and data files.

.. _Wapiti: http://wapiti.limsi.fr/

The idea is to train models with command-line wapiti utility
using templates and training files prepared with WapitiFeatureEncoder,
and then apply this model from Python using WapitiChunker class.
"""

from __future__ import absolute_import
import os
import re
import shlex
import tempfile
from sklearn.base import BaseEstimator, TransformerMixin
from webstruct.utils import get_combined_keys, tostr, run_command
from webstruct.metrics import avg_bio_f1_score


class WapitiCRF(BaseEstimator, TransformerMixin):
    """
    Class for training and applying Wapiti CRF models.

    For training it relies on calling original Wapiti binary (via
    subprocess), so "wapiti" binary must be available if you need "fit"
    method.

    Trained model is saved in an external file; its filename is a first
    parameter to constructor. This file is created and overwritten by
    :meth:`WapitiCRF.fit`; it must exist for :meth:`WapitiCRF.transform`
    to work.

    For prediction WapitiCRF relies on python-wapiti library.
    """

    WAPITI_CMD = 'wapiti'

    def __init__(self, model_filename, train_args=(),
                 feature_template="# Label unigrams and bigrams:\n*\n",
                 unigrams_scope="u", tempdir=None, unlink_temp=True,
                 verbose=True, feature_encoder=None, dev_size=0):

        self.model_filename = model_filename
        if isinstance(train_args, (list, tuple)):
            self.train_args = train_args
        else:
            self.train_args = shlex.split(train_args)
        self.feature_template = feature_template
        self.unigrams_scope = unigrams_scope
        self.tempdir = tempdir
        self.unlink_temp = unlink_temp
        self.verbose = verbose
        self.dev_size = dev_size
        self._wapiti_model = None
        self.feature_encoder = feature_encoder or WapitiFeatureEncoder()
        super(WapitiCRF, self).__init__()

    def fit(self, X, y, X_dev=None, y_dev=None, out_dev=None):
        """
        Train a model.

        Parameters
        ----------
        X: a sequence of lists of feature dicts

        y: a sequence of lists of labels

        X_dev: (optional) a sequence of lists of feature dicts
            Data used for testing and as a stopping criteria.

        y_dev: (optional) a sequence of lists of labels
            Labels corresponding to X_dev.

        out_dev: (optional) string
            Path to a file where tagged development data will be written.

        """
        self._wapiti_model = None
        self.feature_encoder.reset()
        self.feature_encoder.fit(X, y)

        if any([X_dev, y_dev, out_dev]):
            if X_dev is None or y_dev is None:
                raise ValueError("Pass both X_dev and y_dev to use the development data")
        elif self.dev_size > 0:
            # Use a part of training data to help with stopping.
            # It means less data is used for training.
            X_dev, y_dev = X[:self.dev_size], y[:self.dev_size]
            X, y = X[self.dev_size:], y[self.dev_size:]

        dev_fn = None
        to_unlink = []
        try:
            train_fn = self._create_wapiti_data_file(X, y)
            to_unlink.append(train_fn)

            if X_dev is not None:
                dev_fn = self._create_wapiti_data_file(X_dev, y_dev)
                if out_dev is None:
                    _, out_dev = tempfile.mkstemp(dir=self.tempdir, suffix=".txt", prefix="wapiti-dev-data")
                    to_unlink.append(out_dev)

            template_fn = self._create_wapiti_feature_template_file()
            to_unlink.append(template_fn)

            # run wapiti training
            args = ['train', '--pattern', template_fn] + self.train_args
            if dev_fn:
                args += ['--devel', dev_fn]
            args += [train_fn, self.model_filename]
            self.run_wapiti(args)

            # do a final check on development data
            if dev_fn:
                args = ['label', '-m', self.model_filename, '--check', dev_fn, out_dev]
                self.run_wapiti(args)

        finally:
            if self.unlink_temp:
                for filename in to_unlink:
                    os.unlink(filename)

    def transform(self, X):
        """
        Make a prediction.

        Parameters
        ----------
        X: a sequence of lists of feature dicts

        Returns
        -------
        y: a sequence of lists of labels

        """
        model = self._get_python_wapiti_model()
        sequences = self._to_wapiti_sequences(X)
        return [model.label_sequence(seq).splitlines() for seq in sequences]

    def score(self, X, y):
        """
        Macro-averaged F1 score of lists of BIO-encoded sequences
        ``y_true`` and ``y_pred``.

        A named entity in a sequence from ``y_pred`` is considered
        correct only if it is an exact match of the corresponding entity
        in the ``y_true``.

        It requires https://github.com/larsmans/seqlearn to work.
        """
        y_pred = self.transform(X)
        return avg_bio_f1_score(y, y_pred)

    def run_wapiti(self, args):
        return run_command([self.WAPITI_CMD] + args, self.verbose)

    def _get_python_wapiti_model(self):
        if self._wapiti_model is None:
            self._load_model()
        return self._wapiti_model

    def _load_model(self):
        import wapiti
        if self.model_filename is None:
            raise ValueError("model filename is unknown, can't load model")
        self._wapiti_model = wapiti.Model(model=self.model_filename)

    def _to_wapiti_sequences(self, X, y=None):
        X = self.feature_encoder.transform(X)
        if y is None:
            return ["\n".join(lines) for lines in X]
        else:
            return [
                self._to_train_sequence(lines, tags) for lines, tags in zip(X, y)
            ]

    def _create_wapiti_data_file(self, X, y=None):
        """
        Create a file with input data for wapiti. Return a resulting file name;
        caller should unlink the file.
        """
        with tempfile.NamedTemporaryFile('wb', prefix="wapiti-data-", suffix=".txt", dir=self.tempdir, delete=False) as fp:
            for seq in self._to_wapiti_sequences(X, y):
                fp.write(seq.encode('utf8'))
                fp.write(b"\n\n")
        return fp.name

    def _create_wapiti_feature_template_file(self):
        # create feature template
        with tempfile.NamedTemporaryFile('wb', prefix="feature-template-", suffix=".txt", dir=self.tempdir, delete=False) as fp:
            template = self.feature_encoder.prepare_template(self.feature_template)
            fp.write(template.encode('utf8'))

            if self.unigrams_scope is not None:
                unigram_template = self.feature_encoder.unigram_features_template(self.unigrams_scope)
                fp.write(b"\n")
                fp.write(unigram_template.encode('utf8'))
        return fp.name

    def _to_train_sequence(self, wapiti_lines, tags):
        return "\n".join(["%s %s" %(line, tag) for line, tag in zip(wapiti_lines, tags)])


class WapitiFeatureEncoder(BaseEstimator, TransformerMixin):

    def __init__(self, move_to_front=('token',)):
        self.move_to_front = tuple(move_to_front)
        self.feature_names_ = None
        self.vocabulary_ = None

    def fit(self, X, y=None):
        """
        X should be a list of lists of dicts with features;
        It can be obtained using HtmlFeaturesExtractor.
        """
        return self.partial_fit(X)

    def partial_fit(self, X, y=None):
        keys = set(self.feature_names_ or set())
        move_to_front = set(self.move_to_front)

        for feature_dicts in X:
            keys = (keys | get_combined_keys(feature_dicts)) - move_to_front

        self.feature_names_ = self.move_to_front + tuple(keys)
        self.vocabulary_ = dict((f, i) for i, f in enumerate(self.feature_names_))
        return self

    def transform_single(self, feature_dicts):
        """
        Transform a sequence of dicts ``feature_dicts``
        to a list of Wapiti data file lines.
        """
        lines = []
        for dct in feature_dicts:
            line = ' '.join(tostr(dct.get(key)) for key in self.feature_names_)
            lines.append(line)
        return lines

    def transform(self, X):
        return [self.transform_single(feature_dicts) for feature_dicts in X]

    def prepare_template(self, template):
        r"""
        Prepare Wapiti template by replacing feature names with feature
        column indices inside ``%x[row,col]`` macros. Indices are compatible
        with :meth:`WapitiFeatureEncoder.transform` output.

            >>> we = WapitiFeatureEncoder(['token', 'tag'])
            >>> seq_features = [{'token': 'the', 'tag': 'DT'}, {'token': 'dog', 'tag': 'NN'}]
            >>> we.fit([seq_features])
            WapitiFeatureEncoder(move_to_front=('token', 'tag'))
            >>> we.prepare_template('*:Pos-1 L=%x[-1, tag]\n*:Suf-2 X=%m[ 0,token,".?.?$"]')
            '*:Pos-1 L=%x[-1,1]\n*:Suf-2 X=%m[0,0,".?.?$"]'

        Check these links for more info about template format:

        * http://wapiti.limsi.fr/manual.html
        * http://crfpp.googlecode.com/svn/trunk/doc/index.html#templ

        """
        return prepare_wapiti_template(template, self.vocabulary_)

    def unigram_features_template(self, scope='*'):
        """
        Return Wapiti template with unigram features for each of
        known features.

            >>> we = WapitiFeatureEncoder(['token', 'tag'])
            >>> seq_features = [{'token': 'the', 'tag': 'DT'}, {'token': 'dog', 'tag': 'NN'}]
            >>> we.fit([seq_features])
            WapitiFeatureEncoder(move_to_front=('token', 'tag'))
            >>> print(we.unigram_features_template())
            <BLANKLINE>
            # Unigrams for all custom features
            *feat:token=%x[0,0]
            *feat:tag=%x[0,1]
            <BLANKLINE>
            >>> print(we.unigram_features_template('u'))
            <BLANKLINE>
            # Unigrams for all custom features
            ufeat:token=%x[0,0]
            ufeat:tag=%x[0,1]
            <BLANKLINE>
        """
        lines = ['\n# Unigrams for all custom features']
        for col, name in enumerate(self.feature_names_):
            line = '{scope}feat:{name}=%x[0,{col}]'.format(scope=scope, name=name, col=col)
            lines.append(line)
        return "\n".join(lines) + '\n'

    def reset(self):
        self.feature_names_ = None



WAPITI_MACRO_PATTERN = re.compile(r'''
    (?P<macro>%[xXtTmM])
    \[
    \s*(?P<offset>[-]?\d+)\s*
    ,
    \s*(?P<column>[^\],\s]+)\s*  # identifier: anything but closing bracket or comma
    (?P<rest>[\],])              # closing bracket or comma
    ''', re.VERBOSE | re.UNICODE
)

def prepare_wapiti_template(template, vocabulary):
    r"""
    Prepare Wapiti template by replacing feature names with feature
    column indices inside ``%x[row,col]`` macros::

        >>> vocab = {'token': 0, 'tag': 1}
        >>> prepare_wapiti_template('*:Pos-1 L=%x[-1, tag]\n*:Suf-2 X=%m[ 0,token,".?.?$"]', vocab)
        '*:Pos-1 L=%x[-1,1]\n*:Suf-2 X=%m[0,0,".?.?$"]'

    It understands which lines are comments::

        >>> prepare_wapiti_template('*:Pos-1 L=%x[-1, tag]\n# *:Suf-2 X=%m[ 0,token,".?.?$"]', vocab)
        '*:Pos-1 L=%x[-1,1]\n# *:Suf-2 X=%m[ 0,token,".?.?$"]'

    Check these links for more info about template format:

    * http://wapiti.limsi.fr/manual.html
    * http://crfpp.googlecode.com/svn/trunk/doc/index.html#templ

    """
    def repl(m):
        column = m.group('column')
        if not column.isdigit():
            column = vocabulary[column]
        return "{0[macro]}[{0[offset]},{1}{0[rest]}".format(m.groupdict(), column)

    lines = [
        (WAPITI_MACRO_PATTERN.sub(repl, line) if not _wapiti_line_is_comment(line) else line)
        for line in template.splitlines()
    ]

    return "\n".join(lines)


def _wapiti_line_is_comment(line):
    return line.strip().startswith('#')