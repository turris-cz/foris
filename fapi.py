# Foris - web administration interface for OpenWrt based on NETCONF
# Copyright (C) 2013 CZ.NIC, z.s.p.o. <http://www.nic.cz>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

from collections import defaultdict, OrderedDict
from form import Dropdown, Form, Checkbox, websafe, Hidden, Radio
from nuci import client
import logging
from nuci.configurator import add_config_update, commit
from utils import Lazy
import validators as validators_module


logger = logging.getLogger("fapi")


class ForisFormElement(object):
    def __init__(self, name):
        self.name = name
        self.children = OrderedDict()
        self.parent = None
        self.callbacks = []

    def __iter__(self):
        for name in self.children:
            yield self.children[name]

    def _add(self, child):
        self.children[child.name] = child
        child.parent = self
        return child

    def _remove(self, child):
        del self.children[child.name]
        child.parent = None


class ForisForm(ForisFormElement):
    def __init__(self, name, data=None, filter=None):
        """

        :param name:
        :param data: data from request
        :param filter: subtree filter for nuci config
        :type filter: Element
        :return:
        """
        super(ForisForm, self).__init__(name)
        self._request_data = data or {}  # values from request
        self._nuci_data = {}  # values fetched from nuci
        self.defaults = {}  # default values from field definitions
        self.__data_cache = None  # cached data
        self.__form_cache = None
        self.validated = False
        # _nuci_config is not required every time, lazy-evaluate it
        self._nuci_config = Lazy(lambda: client.get(filter))
        self.requirement_map = defaultdict(list)  # mapping: requirement -> list of required_by

    @property
    def sections(self):
        filtered = filter(lambda x: isinstance(x, Section), self.children.itervalues())
        return filtered

    @property
    def data(self):
        """
        Data are union of defaults + nuci values + request data.

        :return: currently known form data
        """
        if self.__data_cache is not None:
            return self.__data_cache
        self._update_nuci_data()
        data = {}
        logger.debug("Updating with defaults: %s" % self.defaults)
        data.update(self.defaults)
        logger.debug("Updating with Nuci data: %s" % self._nuci_data)
        data.update(self._nuci_data)
        logger.debug("Updating with data: %s" % dict(self._request_data))
        data.update(self._request_data)
        data = self.clean_data(data)
        self.__data_cache = data
        return data

    def clean_data(self, data):
        new_data = {}
        fields = self._get_all_fields()
        for field in fields:
            new_data[field.name] = data[field.name]
            if field.name in data:
                if issubclass(field.type, Checkbox):
                    # coerce checkbox values to boolean
                    new_data[field.name] = False if data[field.name] == "0" else bool(data[field.name])
        # get names of active fields according to new_data
        active_field_names = map(lambda x: x.name, self._get_active_fields(data=new_data))
        # get new dict of data of active fields
        return {k: v for k, v in new_data.iteritems() if k in active_field_names}

    def invalidate_data(self):
        self.__data_cache = None

    @property
    def _form(self):
        if self.__form_cache is not None:
            return self.__form_cache
        inputs = map(lambda x: x.field, self._get_active_fields())
        # TODO: creating the form everytime might by a wrong approach...
        logger.debug("Creating Form()...")
        form = Form(*inputs)
        form.fill(self.data)
        self.__form_cache = form
        return form

    @property
    def valid(self):
        return self._form.valid

    def _get_all_fields(self, element=None, fields=None):
        element = element or self
        fields = fields or []
        for c in element.children.itervalues():
            if c.children:
                fields = self._get_all_fields(c, fields)
            if isinstance(c, Field):
                fields.append(c)
        return fields

    def _get_active_fields(self, element=None, data=None):
        """Get all fields that meet their requirements.

        :param element:
        :param data: data to check requirements against
        :return: list of fields
        """
        fields = self._get_all_fields(element)
        data = data or self.data
        return filter(lambda f: f.has_requirements(data), fields)

    def _update_nuci_data(self):
        for field in self._get_all_fields():
            if field.nuci_path:
                value = self._nuci_config.find_child(field.nuci_path)
                if value:
                    self._nuci_data[field.name] = field.nuci_preproc(value)
            elif field.nuci_path:
                NotImplementedError("Cannot map value from Nuci: '%s'" % field.nuci_path)

    def add_section(self, *args, **kwargs):
        """

        :param args:
        :param kwargs:
        :return: new Section
        :rtype: Section
        """
        return self._add(Section(self, *args, **kwargs))

    @property
    def active_fields(self):
        return self._get_active_fields()

    @property
    def errors(self):
        return self._form.note

    def render(self):
        result = "<div class=\"errors\">%s</div>" % self.errors
        result += "\n".join(c.render() for c in self.children.itervalues())
        return result

    def save(self):
        self.process_callbacks(self.data)
        commit()

    def validate(self):
        self.validated = True
        return self._form.validates(self.data)

    def add_callback(self, cb):
        """Add callback function.

        Callback is a function taking argument `data` (contains form data) and returning
        a tuple `(action, *args)`.
        Action can be one of following:
            - edit_config: args is Uci instance - send command for modifying Uci structure
            - none: do nothing, everything has been processed in the callback function

        :param cb: callback function
        :return: None
        """
        self.callbacks.append(cb)

    def process_callbacks(self, form_data):
        logger.debug("Processing callbacks")
        for cb in self.callbacks:
            logger.debug("Processing callback: %s" % cb)
            cb_result = cb(form_data)
            operation = cb_result[0]
            if operation == "none":
                continue
            data = cb_result[1:] if len(cb_result) > 1 else ()
            if operation == "edit_config":
                add_config_update(*data)
            else:
                raise NotImplementedError("Unsupported callback operation: %s" % operation)


class Section(ForisFormElement):
    def __init__(self, main_form, name, title, description=None):
        super(Section, self).__init__(name)
        self._main_form = main_form
        self.name = name
        self.title = title
        self.description = description

    def add_field(self, *args, **kwargs):
        """

        :param args:
        :param kwargs:
        :return:
        :rtype: Field
        """
        return self._add(Field(self._main_form, *args, **kwargs))

    def add_section(self, *args, **kwargs):
        """

        :param args:
        :param kwargs:
        :return: new Section
        :rtype: Section
        """
        return self._add(Section(self._main_form, *args, **kwargs))

    def render(self):
        content = "\n".join(c.render() for c in self.children.itervalues()
                            if c.has_requirements(self._main_form.data))
        return "<section>\n<h2>%(title)s</h2>\n<p>%(description)s</p>\n%(content)s\n</section>" \
               % dict(title=self.title, description=self.description, content=content)


class Field(ForisFormElement):
    def __init__(self, main_form, type, name, label=None, required=False, callback=None, nuci_path=None,
                 nuci_preproc=lambda val: val.value, validators=None, hint="", **kwargs):
        """

        :param main_form: parent form of this field
        :type main_form: ForisForm
        :param type: type of field
        :param name: field name (rendered also as HTML name attribute)
        :param label: display name of field
        :param required: True if field is mandatory
        :param callback: callback for saving the field
        :param nuci_path: path in Nuci get response
        :param nuci_preproc: function to process raw YinElement instance, returns field value
        :type nuci_preproc: callable
        :param validators: validator or list of validators
        :type validators: validator or list
        :param hint: short descriptive text explaining the purpose of the field
        :param kwargs: passed to Input constructor
        """
        super(Field, self).__init__(name)
        #
        self.type = type
        self.name = name
        self.callback = callback
        self.nuci_path = nuci_path
        self.nuci_preproc = nuci_preproc
        if validators and not isinstance(validators, list):
            validators = [validators]
        self.validators = validators or []
        if not all(map(lambda x: isinstance(x, validators_module.Validator), self.validators)):
            raise TypeError("Argument 'validators' must be Validator instance or list of them.")
        self._kwargs = kwargs
        self.required = required
        if self.required:
            self.validators.append(validators_module.NotEmpty())
        self._kwargs["required"] = self.required
        self._kwargs["description"] = label
        self.requirements = {}
        self.hint = hint
        default = kwargs.pop("default", None)
        if issubclass(self.type, Checkbox):
            self._kwargs["value"] = "1"  # we need a non-empty value here
            self._kwargs["checked"] = False if default == "0" else bool(default)
        # set defaults for main form
        self._main_form = main_form
        self._main_form.defaults.setdefault(name, default)
        # cache for rendered field - remove after finishing TODO #2793
        self.__field_cache = None

    def __str__(self):
        return self.render()

    def _generate_html_classes(self):
        classes = []
        if self.name in self._main_form.requirement_map:
            classes.append("has-requirements")
        return classes

    def _generate_html_data(self):
        return validators_module.validators_as_data_dict(self.validators)

    @property
    def field(self):
        if self.__field_cache is not None:
            return self.__field_cache

        validators = self.validators
        # beware, altering self._kwargs might cause funky behaviour
        attrs = self._kwargs.copy()
        # get defined and add generated HTML classes
        classes = attrs.pop("class", "")
        classes = classes.split(" ")
        classes.extend(self._generate_html_classes())
        if classes:
            attrs['class'] = " ".join(classes)
        # append HTML data
        html_data = self._generate_html_data()
        for key, value in html_data.iteritems():
            attrs['data-%s' % key] = value
        # call the proper constructor (web.py Form API is not consistent in this)
        if issubclass(self.type, Dropdown):
            args = attrs.pop("args", ())
            # Dropdowns - signature: def __init__(self, name, args, *validators, **attrs)
            field = self.type(self.name, args, *validators, **attrs)
        else:
            # other - signature: def __init__(self, name, *validators, **attrs)
            field = self.type(self.name, *validators, **attrs)
        if self._main_form.validated:
            field.validate(self._main_form.data or {}, self.name)
        else:
            field.set_value(self._main_form.data.get(self.name) or "")
        if field.note:
            field.attrs['class'] = " ".join(field.attrs['class'].split(" ") + ["field-validation-fail"])
        self.__field_cache = field
        return self.__field_cache

    @property
    def html_id(self):
        return self.field.id

    @property
    def label_tag(self):
        if issubclass(self.type, Radio):
            return "<label>%s</label>" % websafe(self.field.description)

        return "<label for=\"%s\">%s</label>" % (self.field.id, websafe(self.field.description))

    @property
    def errors(self):
        return self.field.note

    @property
    def hidden(self):
        return self.type is Hidden

    def render(self):
        return self.field.render()

    def requires(self, field, value=None):
        """Specify that field requires some other field
        (optionally having some value).

        :param field: name of required field
        :param value: exact value of field
        :return: self
        """
        self._main_form.requirement_map[field].append(self.name)
        self.requirements[field] = value
        return self

    def has_requirements(self, data):
        """Check that this element has its requirements filled in data.

        :param data:
        :return: False if requirements are not met, True otherwise
        """
        for req_name, req_value in self.requirements.iteritems():
            # requirement exists
            result = req_name in data
            # requirement has defined value (value of None is ignored, thus result is True)
            result = result and True if req_value is None else data.get(req_name) == req_value
            if not result:
                return False
        return True
