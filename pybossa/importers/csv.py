# -*- coding: utf8 -*-
# This file is part of PYBOSSA.
#
# Copyright (C) 2015 Scifabric LTD.
#
# PYBOSSA is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# PYBOSSA is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with PYBOSSA.  If not, see <http://www.gnu.org/licenses/>.

import numbers
import types
import requests
from StringIO import StringIO
from flask_babel import gettext
from pybossa.util import unicode_csv_reader, validate_required_fields
from pybossa.util import unicode_csv_reader

from .base import BulkTaskImport, BulkImportException
from flask import request
from werkzeug.datastructures import FileStorage
import io, time, json
from flask import current_app as app
from pybossa.util import get_import_csv_file
import re
from pybossa.data_access import data_access_levels

type_map = {
    # Python considers booleans to be numbers so we need an extra check for that.
    'number': lambda x: isinstance(x, numbers.Real) and type(x) is not bool,
    'bool': lambda x: isinstance(x, bool),
    'null': lambda x: isinstance(x, types.NoneType)
}

def get_value(header, value_string, data_type):
    def error():
        raise BulkImportException('Column {} contains a non-{} value {}'.format(header, data_type, value_string))

    if not data_type:
        return value_string

    try:
        value = json.loads(value_string)
    except ValueError:
        error()

    python_type_checker = type_map.get(data_type)
    if python_type_checker and not python_type_checker(value):
        error()

    return value

class BulkTaskCSVImport(BulkTaskImport):

    """Class to import CSV tasks in bulk."""

    importer_id = "csv"
    reserved_fields = set([
        'state',
        'quorum',
        'calibration',
        'priority_0',
        'n_answers',
        'user_pref',
        'expiration'
    ])

    def __init__(self, csv_url, last_import_meta=None, project=None):
        self.url = csv_url
        self.last_import_meta = last_import_meta
        self.project = project

    def tasks(self):
        """Get tasks from a given URL."""
        dataurl = self._get_data_url()
        r = requests.get(dataurl)
        return self._get_csv_data_from_request(r)

    def headers(self, csvreader=None):
        if self._headers is not None:
            return self._headers
        if not csvreader:
            dataurl = self._get_data_url()
            r = requests.get(dataurl)
            csvreader = self._get_csv_reader_from_request(r)
        self._headers = []
        for row in csvreader:
            self._headers = row
            break
        self._check_no_duplicated_headers()
        self._check_no_empty_headers()
        self._check_required_headers()

        reserved_field_headers = set(self._headers) & reserved_fields
        self.reserved_field_header_index = [
            self._headers.index(field) for field in reserved_field_headers
        ]

        return self._headers

    def _get_data_url(self):
        """Get data from URL."""
        return self.url

    def _convert_row_to_task_data(self, row, row_number):
        task_data = {"info": {}}
        private_fields = dict()
        for idx, cell in enumerate(row):
            header = self._headers[idx]
            if idx in self.reserved_field_header_index:
                if header == 'user_pref':
                    if cell:
                        task_data[header] = json.loads(cell.lower())
                    else:
                        task_data[header] = {}
                elif cell:
                    task_data[header] = cell
                continue

            gold_match = re.match('(?P<field>.*?)(_priv)?_gold(_(?P<type>json|number|bool|null))?$', header)
            if gold_match:
                if cell:
                    data_type = gold_match.group('type')
                    field_name = gold_match.group('field')
                    task_data.setdefault('gold_answers', {})[field_name] = get_value(header, cell, data_type)
                continue

            priv_match = re.match('(?P<field>.*?)_priv(_(?P<type>json|number|bool|null))?$', header)
            if priv_match:
                if cell:
                    data_type = priv_match.group('type')
                    field_name = priv_match.group('field')
                    if data_access_levels: # This is how we check for private GIGwork.
                        private_fields[field_name] = get_value(header, cell, data_type)
                    else:
                        task_data["info"][field_name] = get_value(header, cell, data_type)
                continue

            if header == 'data_access' and data_access_levels:
                if cell:
                    task_data["info"][header] = json.loads(cell.upper())
                continue

            pub_match = re.match('(?P<field>.*?)(_(?P<type>json|number|bool|null))?$', header)
            if pub_match: # This must match since there are no other options left.
                data_type = pub_match.group('type')
                field_name = pub_match.group('field')
                task_data["info"][field_name] = get_value(header, cell, data_type)
        if private_fields:
            task_data['private_fields'] = private_fields
        return task_data

    def _import_csv_tasks(self, csvreader):
        """Import CSV tasks."""
        csviterator = iter(csvreader)
        self.headers(csvreader=csviterator)
        row_number = 0
        for row in csviterator:
            row_number += 1
            self._check_valid_row_length(row, row_number)

            # check required fields
            fvals = {self._headers[idx]: cell for idx, cell in enumerate(row)}
            invalid_fields = validate_required_fields(fvals)
            if invalid_fields:
                msg = gettext('The file you uploaded has incorrect/missing '
                                'values for required header(s): {0}'
                                .format(','.join(invalid_fields)))
                raise BulkImportException(msg)
            task_data = self._convert_row_to_task_data(row, row_number)
            task_state = task_data.get('state')
            if task_state not in ['enrich', 'ongoing', None]:
                raise BulkImportException('Invalid task state: {}'.format(task_state))

            if self.project and task_state == 'enrich':
                enrichments = project.info.get('enrichments')
                if not enrichments:
                    raise BulkImportException('No enrichment settings configured. Task state of "enrich" not allowed.')
                enrichment_fields_in_import = [
                    enrichment.get('out_field_name') for enrichment in enrichments
                    if enrichment.get('out_field_name') in self._headers
                ]
                if enrichment_fields_in_import:
                    raise BulkImportException('Enrichment output field is in import: {}'.format(', '.join(enrichment_fields_in_import)))
            yield task_data

    def _check_no_duplicated_headers(self):
        if len(self._headers) != len(set(self._headers)):
            msg = gettext('The file you uploaded has '
                          'two headers with the same name.')
            raise BulkImportException(msg)

    def _check_no_empty_headers(self):
        stripped_headers = [header.strip() for header in self._headers]
        if "" in stripped_headers:
            position = stripped_headers.index("")
            msg = gettext("The file you uploaded has an empty header on "
                          "column %(pos)s.", pos=(position+1))
            raise BulkImportException(msg)

    def _check_valid_row_length(self, row, row_number):
        if len(self._headers) != len(row):
            msg = gettext("The file you uploaded has an extra value on "
                          "row %s." % (row_number+1))
            raise BulkImportException(msg)

    def _check_required_headers(self):
        required_headers = app.config.get("TASK_REQUIRED_FIELDS", {})
        missing_headers = [r for r in required_headers if r not in self._headers]
        if missing_headers:
            msg = gettext('The file you uploaded has missing '
                          'required header(s): {0}'.format(','.join(missing_headers)))
            raise BulkImportException(msg)

    def _get_csv_reader_from_request(self, r):
        """Get CSV data from a request."""
        if r.status_code == 403:
            msg = ("Oops! It looks like you don't have permission to access"
                   " that file")
            raise BulkImportException(gettext(msg), 'error')
        if (('text/plain' not in r.headers['content-type']) and
                ('text/csv' not in r.headers['content-type'])):
            msg = gettext("Oops! That file doesn't look like the right file.")
            raise BulkImportException(msg, 'error')

        r.encoding = 'utf-8'
        csvcontent = StringIO(r.text)
        return unicode_csv_reader(csvcontent)

    def _get_csv_data_from_request(self, r):
        return self._import_csv_tasks(self._get_csv_reader_from_request(r))

class BulkTaskGDImport(BulkTaskCSVImport):

    """Class to import tasks from Google Drive in bulk."""

    importer_id = "gdocs"

    def __init__(self, googledocs_url):
        self.url = googledocs_url

    def _get_data_url(self, **form_data):
        """Get data from URL."""
        # For old data links of Google Spreadsheets
        if 'ccc?key' in self.url:
            return ''.join([self.url, '&output=csv'])
        # New data format for Google Drive import is like this:
        # https://docs.google.com/spreadsheets/d/key/edit?usp=sharing
        else:
            return ''.join([self.url.split('edit')[0],
                            'export?format=csv'])


class BulkTaskLocalCSVImport(BulkTaskCSVImport):

    """Class to import CSV tasks in bulk from local file."""

    importer_id = "localCSV"

    def __init__(self, **form_data):
       self.form_data = form_data

    def _get_data(self):
        """Get data."""
        return self.form_data['csv_filename']

    def count_tasks(self):
        return len([task for task in self.tasks()])

    def _get_csv_data_from_request(self, csv_filename):
        if csv_filename is None:
            msg = ("Not a valid csv file for import")
            raise BulkImportException(gettext(msg), 'error')

        datafile = get_import_csv_file(csv_filename)
        csv_file = FileStorage(io.open(datafile.name, encoding='utf-8-sig'))    #utf-8-sig to ignore BOM

        if csv_file is None or csv_file.stream is None:
            msg = ("Unable to load csv file for import, file {0}".format(csv_filename))
            raise BulkImportException(gettext(msg), 'error')

        csv_file.stream.seek(0)
        csvcontent = io.StringIO(csv_file.stream.read())
        csvreader = unicode_csv_reader(csvcontent)
        return list(self._import_csv_tasks(csvreader))

    def tasks(self):
        """Get tasks from a given URL."""
        csv_filename = self._get_data()
        return self._get_csv_data_from_request(csv_filename)
