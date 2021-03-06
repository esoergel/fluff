from collections import defaultdict
from couchdbkit import ResourceNotFound
from couchdbkit.ext.django import schema
import datetime
from dimagi.utils.parsing import json_format_date
from dimagi.utils.read_only import ReadOnlyObject
from fluff import exceptions
from pillowtop.listener import BasicPillow
from .signals import indicator_document_updated
import fluff.sync_couchdb


REDUCE_TYPES = set(['sum', 'count', 'min', 'max', 'sumsqr'])
TYPE_INTEGER = 'integer'
TYPE_STRING = 'string'
TYPE_DATE = 'date'
ALL_TYPES = [TYPE_INTEGER, TYPE_STRING, TYPE_DATE]


class base_emitter(object):
    fluff_emitter = ''

    def __init__(self, reduce_type='sum'):
        assert reduce_type in REDUCE_TYPES, 'Unknown reduce type'
        self.reduce_type = reduce_type

    def __call__(self, fn):
        def wrapped_f(*args):
            for v in fn(*args):
                if isinstance(v, dict):
                    if 'value' not in v:
                        v['value'] = 1
                    assert v.get('group_by') is not None
                    if not isinstance(v['group_by'], list):
                        v['group_by'] = [v['group_by']]
                elif isinstance(v, list):
                    v = dict(date=v[0], value=v[1], group_by=None)
                else:
                    v = dict(date=v, value=1, group_by=None)

                self.validate(v)
                yield v

        wrapped_f._reduce_type = self.reduce_type
        wrapped_f._fluff_emitter = self.fluff_emitter
        return wrapped_f

    def validate(self, value):
        pass


class custom_date_emitter(base_emitter):
    fluff_emitter = 'date'

    def validate(self, value):
        def validate_date(dateval):
            assert dateval is not None
            assert isinstance(dateval, (datetime.date, datetime.datetime))

        validate_date(value.get('date'))
        if isinstance(value['date'], datetime.datetime):
            value['date'] = value['date'].date()


class custom_null_emitter(base_emitter):
    fluff_emitter = 'null'

    def validate(self, value):
        if isinstance(value, dict):
            if 'date' not in value:
                value['date'] = None
            else:
                assert value['date'] is None

date_emitter = custom_date_emitter()
null_emitter = custom_null_emitter()


def filter_by(fn):
    fn._fluff_filter = True
    return fn


class CalculatorMeta(type):
    _counter = 0

    def __new__(mcs, name, bases, attrs):
        emitters = set()
        filters = set()
        parents = [p for p in bases if isinstance(p, CalculatorMeta)]
        for attr in attrs:
            if getattr(attrs[attr], '_fluff_emitter', None):
                emitters.add(attr)
            if getattr(attrs[attr], '_fluff_filter', False):
                filters.add(attr)

        # needs to inherit emitters and filters from all parents
        for parent in parents:
            emitters.update(parent._fluff_emitters)
            filters.update(parent._fluff_filters)

        cls = super(CalculatorMeta, mcs).__new__(mcs, name, bases, attrs)
        cls._fluff_emitters = emitters
        cls._fluff_filters = filters
        cls._counter = mcs._counter
        mcs._counter += 1
        return cls


class Calculator(object):
    __metaclass__ = CalculatorMeta

    window = None

    # set by IndicatorDocumentMeta
    fluff = None
    slug = None

    # set by CalculatorMeta
    _fluff_emitters = None
    _fluff_filters = None

    def __init__(self, window=None, filter=None):
        if window is not None:
            self.window = window
        if not isinstance(self.window, datetime.timedelta):
            if any(getattr(self, e)._fluff_emitter == 'date' for e in self._fluff_emitters):
                # if window is set to None, for instance
                # fail here and not whenever that's run into below
                raise NotImplementedError(
                    'window must be timedelta, not %s' % type(self.window))
        self._filter = filter

    def filter(self, item):
        return self._filter is None or self._filter.filter(item)

    def passes_filter(self, item):
        """
        This is pretty confusing, but there are two mechanisms for having a filter,
        one via the explicit filter function and the other being the @filter_by decorator
        that can be applied to other functions.
        """
        return self.filter(item) and all(
            (getattr(self, slug)(item) for slug in self._fluff_filters)
        )

    def to_python(self, value):
        return value

    def calculate(self, item):
        passes_filter = self.passes_filter(item)
        values = {}
        for slug in self._fluff_emitters:
            fn = getattr(self, slug)
            values[slug] = (
                list(fn(item))
                if passes_filter else []
            )
        return values

    def get_result(self, key, reduce=True):
        result = {}
        for emitter_name in self._fluff_emitters:
            shared_key = [self.fluff._doc_type] + key + [self.slug, emitter_name]
            emitter = getattr(self, emitter_name)
            emitter_type = emitter._fluff_emitter
            q_args = {
                'reduce': reduce,
            }
            if emitter_type == 'date':
                now = self.fluff.get_now()
                start = now - self.window
                end = now
                if start > end:
                    q_args['descending'] = True
                q = self.fluff.view(
                    'fluff/generic',
                    startkey=shared_key + [json_format_date(start)],
                    endkey=shared_key + [json_format_date(end)],
                    **q_args
                ).all()
            elif emitter_type == 'null':
                q = self.fluff.view(
                    'fluff/generic',
                    key=shared_key + [None],
                    **q_args
                ).all()
            else:
                raise exceptions.EmitterTypeError(
                    'emitter type %s not recognized' % emitter_type
                )

            if reduce:
                try:
                    result[emitter_name] = q[0]['value'][emitter._reduce_type]
                except IndexError:
                    result[emitter_name] = 0
            else:
                def strip(id_string):
                    prefix = '%s-' % self.fluff.__name__
                    assert id_string.startswith(prefix)
                    return id_string[len(prefix):]
                result[emitter_name] = [strip(row['id']) for row in q]
        return result

    def aggregate_results(self, keys, reduce=True):

        def iter_results():
            for key in keys:
                result = self.get_result(key, reduce=reduce)
                for slug, value in result.items():
                    yield slug, value

        if reduce:
            results = defaultdict(int)
            for slug, value in iter_results():
                results[slug] += value
        else:
            results = defaultdict(set)
            for slug, value in iter_results():
                results[slug].update(value)

        return results

class AttributeGetter(object):
    """
    If you need to do something fancy in your group_by you would use this.
    """
    def __init__(self, attribute, getter_function=None):
        """
        attribute is what the attribute is set as in the fluff indicator doc.
        getter_function is how to get it out of the source doc.
        if getter_function isn't specified it will use source[attribute] as
        the getter.
        """
        self.attribute = attribute
        if getter_function is None:
            getter_function = lambda item: item[attribute]

        self.getter_function = getter_function


class IndicatorDocumentMeta(schema.DocumentMeta):

    def __new__(mcs, name, bases, attrs):
        calculators = {}
        for attr_name, attr_value in attrs.items():
            if isinstance(attr_value, Calculator):
                calculators[attr_name] = attr_value
                attrs[attr_name] = schema.DictProperty()
        cls = super(IndicatorDocumentMeta, mcs).__new__(mcs, name, bases, attrs)
        for slug, calculator in calculators.items():
            calculator.fluff = cls
            calculator.slug = slug
        cls._calculators = calculators
        return cls


class IndicatorDocument(schema.Document):

    __metaclass__ = IndicatorDocumentMeta
    base_doc = 'IndicatorDocument'

    document_class = None
    wrapper = None
    document_filter = None
    group_by = ()

    # Mapping of group_by field to type. Used to communicate expected type in fluff diffs.
    # See ALL_TYPES
    group_by_type_map = None

    @property
    def wrapped_group_by(self):
        def _wrap_if_necessary(string_or_attribute_getter):
            if isinstance(string_or_attribute_getter, basestring):
                getter = AttributeGetter(string_or_attribute_getter)
            else:
                getter = string_or_attribute_getter
            assert isinstance(getter, AttributeGetter)
            return getter

        return (_wrap_if_necessary(item) for item in type(self)().group_by)

    def get_group_names(self):
        return [gb.attribute for gb in self.wrapped_group_by]

    def get_group_values(self):
        return [self[attr] for attr in self.get_group_names()]

    @classmethod
    def get_now(cls):
        return datetime.datetime.utcnow().date()

    def calculate(self, item):
        for attr, calculator in self._calculators.items():
            self[attr] = calculator.calculate(item)
        self.id = item.get_id
        for getter in self.wrapped_group_by:
            self[getter.attribute] = getter.getter_function(item)
        # overwrite whatever's in group_by with the default
        self._doc['group_by'] = list(self.get_group_names())

    def diff(self, other_doc):
        """
        Get the diff between two IndicatorDocuments. Assumes that the documents are of the same type and that
        both have the same set of calculators and emitters. Doesn't support changes to group_by values.

        Return value is None for no diff or a dict with all indicator values
        that are different (added / removed / changed):
            {
                domains: ['domain1', 'domain2']
                database: 'db1',
                doc_type: 'MyIndicators',
                group_names: ['domain', 'owner_id'],
                group_values: ['test', 'abc']
                indicator_changes: [
                    {
                    calculator: 'visit_week',
                    emitter: 'all_visits',
                    emitter_type: 'date',
                    reduce_type: 'count',
                    values: [
                        {'date': '2012-09-23', 'value': 1, 'group_by': None},
                        {'date': '2012-09-24', 'value': 1, 'group_by': None}
                    ]},
                    {
                    calculator: 'visit_week',
                    emitter: 'visit_hour',
                    emitter_type: 'date',
                    reduce_type: 'sum',
                    values: [
                        {'date': '2012-09-23', 'value': 8, 'group_by': None},
                        {'date': '2012-09-24', 'value': 11, 'group_by': None}
                    ]},
                ],
                all_indicators: [
                    {
                    calculator: 'visit_week',
                    emitter: 'visit_hour',
                    emitter_type: 'date',
                    reduce_type: 'sum'
                    },
                    ....
                ]

            }
        """
        diff_keys = {}
        for calc_name in self._calculators.keys():
            if other_doc:
                calc_diff = self._shallow_dict_diff(self[calc_name], other_doc[calc_name])
                if calc_diff:
                    diff_keys[calc_name] = calc_diff
            else:
                for emitter_name, values in self[calc_name].items():
                    if values:
                        emitters = diff_keys.setdefault(calc_name, [])
                        emitters.append(emitter_name)

        if not diff_keys:
            return None

        group_by_type_map = self.group_by_type_map or {}
        for gb in self.wrapped_group_by:
            attrib = gb.attribute
            if attrib not in group_by_type_map:
                group_by_type_map[attrib] = TYPE_STRING
            else:
                assert group_by_type_map[attrib] in ALL_TYPES

        diff = dict(domains=list(self.domains),
                    database=self.Meta.app_label,
                    doc_type=self._doc_type,
                    group_names=self.get_group_names(),
                    group_values=self.get_group_values(),
                    group_type_map=group_by_type_map,
                    indicator_changes=[],
                    all_indicators=[])
        indicator_changes = diff["indicator_changes"]
        all_indicators = diff["all_indicators"]

        for calc_name, emitter_names in diff_keys.items():
            indicator_changes.extend(self._indicator_diff(calc_name, emitter_names, other_doc))

        for calc_name in self._calculators.keys():
            for emitter_name in self[calc_name].keys():
                all_indicators.append(self._indicator_meta(calc_name, emitter_name))

        return diff

    def _indicator_meta(self, calc_name, emitter_name, values=None):
        emitter = getattr(self._calculators[calc_name], emitter_name)
        emitter_type = emitter._fluff_emitter
        reduce_type = emitter._reduce_type
        meta = dict(calculator=calc_name,
           emitter=emitter_name,
           emitter_type=emitter_type,
           reduce_type=reduce_type
        )

        if values is not None:
            meta['values'] = values

        return meta

    def _indicator_diff(self, calc_name, emitter_names, other_doc):
        indicators = []
        for emitter_name in emitter_names:
            class NormalizedEmittedValue(object):
                """Normalize the values to the dictionary form to allow comparison"""
                def __init__(self, value):
                    if isinstance(value, dict):
                        self.value = value
                    elif isinstance(value, list):
                        self.value = dict(date=value[0], value=value[1], group_by=None)

                    if self.value['date'] and not isinstance(self.value['date'], datetime.date):
                        self.value['date'] = datetime.datetime.strptime(self.value['date'], '%Y-%m-%d').date()

                def __key(self):
                    gb = self.value['group_by']
                    return self.value['date'], self.value['value'], tuple(gb) if gb else None

                def __eq__(x, y):
                    return x.__key() == y.__key()

                def __hash__(self):
                    return hash(self.__key())

                def __repr__(self):
                    return str(self.value)

            if other_doc:
                self_values = set([NormalizedEmittedValue(v) for v in self[calc_name][emitter_name]])
                other_values = set([NormalizedEmittedValue(v) for v in other_doc[calc_name][emitter_name]])
                values_diff = [v for v in list(self_values - other_values)]
            else:
                values_diff = [NormalizedEmittedValue(v) for v in self[calc_name][emitter_name]]

            values = [v.value for v in values_diff]
            indicators.append(self._indicator_meta(calc_name, emitter_name, values=values))
        return indicators

    def _shallow_dict_diff(self, left, right):
        if not left and not right:
            return None
        elif not left or not right:
            return left.keys() if left else right.keys()

        left_set, right_set = set(left.keys()), set(right.keys())
        intersect = right_set.intersection(left_set)

        added = right_set - intersect
        removed = left_set - intersect
        changed = set(o for o in intersect if left[o] != right[o])
        return added | removed | changed

    @classmethod
    def pillow(cls):
        wrapper = cls.wrapper or cls.document_class
        doc_type = cls.document_class._doc_type
        extra_args = dict(doc_type=doc_type)
        if cls.domains:
            domains = ' '.join(cls.domains)
            extra_args['domains'] = domains

        document_filter = cls.document_filter
        return type(FluffPillow)(cls.__name__ + 'Pillow', (FluffPillow,), {
            'couch_filter': 'fluff_filter/domain_type',
            'extra_args': extra_args,
            'document_class': cls.document_class,
            'wrapper': wrapper,
            'indicator_class': cls,
            'document_filter': document_filter,
        })

    @classmethod
    def has_calculator(cls, calc_name):
        return calc_name in cls._calculators

    @classmethod
    def get_calculator(cls, calc_name):
        return cls._calculators[calc_name]

    @classmethod
    def get_result(cls, calc_name, key, reduce=True):
        calculator = cls.get_calculator(calc_name)
        return calculator.get_result(key, reduce=reduce)

    @classmethod
    def aggregate_results(cls, calc_name, keys, reduce=True):
        calculator = cls.get_calculator(calc_name)
        return calculator.aggregate_results(keys, reduce=reduce)

    class Meta:
        app_label = 'fluff'


class FluffPillow(BasicPillow):
    document_filter = None
    wrapper = None
    indicator_class = IndicatorDocument

    def change_transform(self, doc_dict):
        doc = self.wrapper.wrap(doc_dict)
        doc = ReadOnlyObject(doc)

        if self.document_filter and not self.document_filter.filter(doc):
            return None

        indicator_id = '%s-%s' % (self.indicator_class.__name__, doc.get_id)

        try:
            current_indicator = self.indicator_class.get(indicator_id)
        except ResourceNotFound:
            current_indicator = None

        if not current_indicator:
            indicator = self.indicator_class(_id=indicator_id)
        else:
            indicator = current_indicator
            current_indicator = indicator.to_json()

        indicator.calculate(doc)
        return current_indicator, indicator

    def change_transport(self, indicators):
        old_indicator, new_indicator = indicators
        new_indicator.save()

        diff = new_indicator.diff(old_indicator)
        if diff:
            indicator_document_updated.send(sender=self, diff=diff)
