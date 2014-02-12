# -*- coding: utf-8 -*-
from __future__ import absolute_import
import copy
from itertools import chain
from collections import namedtuple, Counter
from sklearn.base import BaseEstimator, TransformerMixin
from webstruct.sequence_encoding import IobEncoder
from webstruct.tokenizers import tokenize
from webstruct.features import CombinedFeatures
from webstruct.utils import replace_html_tags, kill_html_tags

_HtmlToken = namedtuple('HtmlToken', 'index tokens elem is_tail')

class HtmlToken(_HtmlToken):
    """
    HTML token info.

    * ``index`` is a token index (in the ``tokens`` list);
    * ``tokens`` is a list of all tokens in current html block;
    * ``elem`` is the current html block (as lxml's Element) - most likely
      you want ``HtmlToken.parent`` instead of it;
    * ``is_tail`` flag that indicates that token belongs to element tail

    Computed properties:

    * ``token`` is the current token (as text);
    * ``parent`` is token's parent HTML element (as lxml's Element).

    """
    @property
    def token(self):
        return self.tokens[self.index]

    @property
    def parent(self):
        if not self.is_tail:
            return self.elem
        return self.elem.getparent()


class HtmlTokenizer(object):
    """
    Use ``HtmlTokenizer.tokenize_single`` to convert HTML tree (returned by one
    of the webstruct loaders) to lists of HtmlToken instances
    and associated tags::

        >>> from webstruct import GateLoader, HtmlTokenizer
        >>> loader = GateLoader(known_tags=['PER'])
        >>> html_tokenizer = HtmlTokenizer(replace_html_tags={'b': 'strong'})
        >>> tree = loader.loadbytes(b"<p>hello, <PER>John <b>Doe</b></PER> <br> <PER>Mary</PER> said</p>")
        >>> html_tokens, tags = html_tokenizer.tokenize_single(tree)
        >>> html_tokens  # doctest: +ELLIPSIS
        [HtmlToken(index=0, tokens=[u'hello', u'John'], elem=<Element p at ...>, is_tail=False), HtmlToken...]
        >>> tags
        ['O', u'B-PER', u'I-PER', u'B-PER', 'O']
        >>> for tok, iob_tag in zip(html_tokens, tags):
        ...     print "%5s" % iob_tag, tok.token, tok.elem.tag, tok.parent.tag
            O hello p p
        B-PER John p p
        I-PER Doe strong strong
        B-PER Mary br p
            O said br p

    For HTML without text it returns empty lists::

        >>> html_tokenizer.tokenize_single(loader.loadbytes(b'<p></p>'))
        ([], [])

    """
    def __init__(self, tagset=None, sequence_encoder=None, text_tokenize_func=None,
                 kill_html_tags=None, replace_html_tags=None):
        self.tagset = set(tagset) if tagset is not None else None
        self.text_tokenize_func = text_tokenize_func or tokenize
        self.kill_html_tags = kill_html_tags
        self.replace_html_tags = replace_html_tags

        # FIXME: don't use shared instance of sequence encoder
        self.sequence_encoder_ = sequence_encoder or IobEncoder()

    def tokenize_single(self, tree):
        """
        Return two lists:

        * a list a list of HtmlToken tokens;
        * a list of associated tags.

        For unannotated HTML all tags will be "O" - they may be ignored.
        """
        tree = copy.deepcopy(tree)
        self.sequence_encoder_.reset()
        self._prepare_tree(tree)
        res = zip(*(self._process_tree(tree)))
        if not res:
            return ([], [])
        return list(res[0]), list(res[1])

    def tokenize(self, trees):
        X, y = [], []
        for tree in trees:
            html_tokens, tags = self.tokenize_single(tree)
            X.append(html_tokens)
            y.append(tags)
        return X, y

    def _prepare_tree(self, tree):
        if self.kill_html_tags:
            kill_html_tags(tree, self.kill_html_tags, keep_child=True)

        if self.replace_html_tags:
            replace_html_tags(tree, self.replace_html_tags)

    def _process_tree(self, tree):
        head_tokens, head_tags = self._tokenize_and_split(tree.text)
        for index, (token, tag) in enumerate(zip(head_tokens, head_tags)):
            yield HtmlToken(index, head_tokens, tree, False), tag

        for child in tree:  # where is my precious "yield from"?
            for html_token, tag in self._process_tree(child):
                yield html_token, tag

        tail_tokens, tail_tags = self._tokenize_and_split(tree.tail)
        for index, (token, tag) in enumerate(zip(tail_tokens, tail_tags)):
            yield HtmlToken(index, tail_tokens, tree, True), tag

    def _tokenize_and_split(self, text):
        input_tokens = self._limit_tags(self.text_tokenize_func(text or ''))
        input_tokens = map(unicode, input_tokens)
        return self.sequence_encoder_.encode_split(input_tokens)

    def _limit_tags(self, input_tokens):
        if self.tagset is None:
            return input_tokens

        proc = self.sequence_encoder_.token_processor_
        token_classes = [proc.classify(tok) for tok in input_tokens]
        return [
            tok for (tok, (typ, value)) in zip(input_tokens, token_classes)
            if not (typ in {'start', 'end'} and value not in self.tagset)
        ]


class HtmlFeatureExtractor(BaseEstimator, TransformerMixin):
    """
    This class extracts features from lists of HtmlTokens
    (from html trees tokenized using HtmlTokenizer).

    Parameters
    ----------

    token_features : list of callables
        List of "token" feature functions. Each function accepts
        a single ``html_token`` parameter and returns a dictionary
        wich maps feature names to feature values. Dicts from all
        token feature functions are merged by HtmlFeatureExtractor.
        Example token feature (it just returns token text)::

            >>> def current_token(html_token):
            ...     return {'tok': html_token.token}

        ``webstruct.features`` module provides some predefined feature
        functions, e.g. ``parent_tag`` which returns token's parent tag.

        Example::

            >>> from webstruct import GateLoader, HtmlTokenizer, HtmlFeatureExtractor
            >>> from webstruct.features import parent_tag

            >>> loader = GateLoader(known_tags=['PER'])
            >>> html_tokenizer = HtmlTokenizer()
            >>> feature_extractor = HtmlFeatureExtractor(token_features=[parent_tag])

            >>> tree = loader.loadbytes(b"<p>hello, <PER>John <b>Doe</b></PER> <br> <PER>Mary</PER> said</p>")
            >>> html_tokens, tags = html_tokenizer.tokenize_single(tree)
            >>> feature_dicts = feature_extractor.transform_single(html_tokens)
            >>> for token, tag, feat in zip(html_tokens, tags, feature_dicts):
            ...     print("%s %s %s" % (token.token, tag, feat))
            hello O {'parent_tag': 'p'}
            John B-PER {'parent_tag': 'p'}
            Doe I-PER {'parent_tag': 'b'}
            Mary B-PER {'parent_tag': 'p'}
            said O {'parent_tag': 'p'}

    global_features : list of callables, optional
        List of "global" feature functions. Each "global" feature function
        accepts a list of (``html_token``, ``feature_dict``) tuples.
        "Global" feature functions should change ``feature_dict``s inplace.

    min_df : integer of Mapping, optional
        Feature values that have a document frequency strictly
        lower than the given threshold are removed.
        If ``min_df`` is integer, its value is used as threshold.

        TODO: if ``min_df`` is a dictionary, it should map feature names
        to thresholds.

    """
    def __init__(self, token_features, global_features=None, min_df=1):
        self.token_features = token_features
        self.global_features = global_features or []
        self.min_df = min_df

    def fit(self, htmltoken_lists, y=None):
        self.fit_transform(htmltoken_lists)
        return self

    def fit_transform(self, htmltoken_lists, y=None, **fit_params):
        X = [self.transform_single(html_tokens) for html_tokens in htmltoken_lists]
        return self._pruned(X, low=self.min_df)

    def transform(self, htmltoken_lists):
        return [self.transform_single(html_tokens) for html_tokens in htmltoken_lists]

    def transform_single(self, html_tokens):
        feature_func = CombinedFeatures(*self.token_features)
        token_data = list(zip(html_tokens, map(feature_func, html_tokens)))

        for feat in self.global_features:
            feat(token_data)

        return [{k: fd[k] for k in fd if not k.startswith('_')}
                for tok, fd in token_data]

    def _pruned(self, X, low=None):
        if low is None or low <= 1:
            return X
        cnt = self._document_frequency(X)
        keep = {k for (k, v) in cnt.items() if v >= low}
        del cnt
        return [
            [{k: v for k, v in fd.items() if (k, v) in keep} for fd in doc]
            for doc in X
        ]

    def _document_frequency(self, X):
        cnt = Counter()
        for doc in X:
            seen_features = set(chain.from_iterable(fd.items() for fd in doc))
            cnt.update(seen_features)
        return cnt