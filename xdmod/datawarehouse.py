from collections.abc import Mapping
import csv
from datetime import date, datetime, timedelta
import html
import io
import json
import numpy
import os
import pandas as pd
import pycurl
import re
import tempfile
from urllib.parse import urlencode


class DataWarehouse:
    """Access the XDMoD data warehouse via XDMoD's network API.

       Methods must be called within a runtime context using the ``with``
       keyword, e.g.,

       >>> with DataWarehouse(XDMOD_URL, XDMOD_API_TOKEN) as x:
       ...     x.get_aggregate_data()

       Parameters
       ----------
       xdmod_host : str
           The URL of the XDMoD server.
       api_token : str, optional
           The API token used to connect. If not provided, the
           `XDMOD_USER` and `XDMOD_PASS` environment variables must be
           set.

       Raises
       ------
       KeyError
           If `api_token` is None and either or both of the environment
           variables `XDMOD_USER` and `XDMOD_PASS` have not been set.
       TypeError
           If `xdmod_host` is not a string or if `api_token` is not None and is
           not a string.
       RuntimeError
           If a connection cannot be made to the XDMoD server specified by
           `xdmod_host`.
    """
    def __init__(self, xdmod_host, api_token=None):
        self.__assert_str('xdmod_host', xdmod_host)
        self.__xdmod_host = xdmod_host

        if api_token:
            self.__assert_str('api_token', api_token)
        self.__api_token = api_token

        self.__in_runtime_context = False
        self.__username = None
        self.__crl = None
        self.__cookie_file = None
        self.__descriptor = None
        self.__headers = []

        self.__init_api_token()
        self.__init_valid_values()
        self.__init_dates()

    def __assert_str(self, name, value):
        if not isinstance(value, str):
            raise TypeError('`' + name + '` must be a string.')

    def __init_api_token(self):
        if not self.__api_token:
            username = self.__get_environment_variable('XDMOD_USER')
            password = self.__get_environment_variable('XDMOD_PASS')
            self.__api_token = {'username': username,
                                'password': password}

    def __get_environment_variable(self, name):
        try:
            value = os.environ[name]
        except KeyError:
            raise KeyError(name + ' environment variable'
                           + ' has not been set.') from None
        return value

    def __init_valid_values(self):
        self.__valid_values = {}

        this_year = date.today().year
        six_years_ago = this_year - 6
        last_seven_years = tuple(map(str, reversed(range(six_years_ago,
                                                         this_year + 1))))

        self.__valid_values['duration'] = (('Yesterday',
                                            '7 day',
                                            '30 day',
                                            '90 day',
                                            'Month to date',
                                            'Previous month',
                                            'Quarter to date',
                                            'Previous quarter',
                                            'Year to date',
                                            'Previous year',
                                            '1 year',
                                            '2 year',
                                            '3 year',
                                            '5 year',
                                            '10 year')
                                           + last_seven_years)

        self.__valid_values['aggregation_unit'] = ('Auto',
                                                   'Day',
                                                   'Month',
                                                   'Quarter',
                                                   'Year')

    def __init_dates(self):
        today = date.today()
        yesterday = today + timedelta(days=-1)
        last_week = today + timedelta(days=-7)
        last_month = today + timedelta(days=-30)
        last_quarter = today + timedelta(days=-90)
        this_month_start = date(today.year, today.month, 1)

        if today.month == 1:
            last_full_month_start_year = today.year - 1
            last_full_month_start_month = 12
        else:
            last_full_month_start_year = today.year
            last_full_month_start_month = today.month - 1

        last_full_month_start = date(last_full_month_start_year,
                                     last_full_month_start_month,
                                     1)

        last_full_month_end = this_month_start + timedelta(days=-1)
        this_quarter_start = date(today.year,
                                  ((today.month - 1) // 3) * 3 + 1,
                                  1)

        if today.month < 4:
            last_quarter_start_year = today.year - 1
        else:
            last_quarter_start_year = today.year
        last_quarter_start = date(
            last_quarter_start_year,
            (((today.month - 1) - ((today.month - 1) % 3) + 9) % 12) + 1,
            1)

        last_quarter_end = this_quarter_start + timedelta(days=-1)
        this_year_start = date(today.year, 1, 1)
        previous_year_start = date(today.year - 1, 1, 1)
        previous_year_end = date(today.year - 1, 12, 31)

        self.__DURATION_TO_START_END = {
            'Yesterday': (yesterday, yesterday),
            '7 day': (last_week, today),
            '30 day': (last_month, today),
            '90 day': (last_quarter, today),
            'Month to date': (this_month_start, today),
            'Previous month': (last_full_month_start, last_full_month_end),
            'Quarter to date': (this_quarter_start, today),
            'Previous quarter': (last_quarter_start, last_quarter_end),
            'Year to date': (this_year_start, today),
            'Previous year': (previous_year_start, previous_year_end),
            '1 year': (self.__date_add_years(today, -1), today),
            '2 year': (self.__date_add_years(today, -2), today),
            '3 year': (self.__date_add_years(today, -3), today),
            '5 year': (self.__date_add_years(today, -5), today),
            '10 year': (self.__date_add_years(today, -10), today)}

    def __date_add_years(self, old_date, year_delta):
        # Make dates behave like Ext.JS, i.e., if a date is specified
        # with a day value that is too big, add days to the last valid
        # day in that month, e.g., 2023-02-31 becomes 2023-03-03.
        new_date_year = old_date.year + year_delta
        new_date_day = old_date.day
        days_above = 0
        keep_going = True
        while keep_going:
            try:
                new_date = date(new_date_year, old_date.month, new_date_day)
                keep_going = False
            except ValueError:
                new_date_day -= 1
                days_above += 1
        return new_date + timedelta(days=days_above)

    def __enter__(self):
        self.__in_runtime_context = True
        self.__crl = pycurl.Curl()

        self.__assert_connection_to_xdmod_host()

        if self.__api_token:
            _, self.__cookie_file = tempfile.mkstemp()
            self.__crl.setopt(pycurl.COOKIEJAR, self.__cookie_file)
            self.__crl.setopt(pycurl.COOKIEFILE, self.__cookie_file)

            response = self.__request_json('/rest/auth/login',
                                           self.__api_token)

            if response['success'] is True:
                token = response['results']['token']
                self.__headers = ['Token: ' + token]
                self.__crl.setopt(pycurl.HTTPHEADER, self.__headers)
                self.__username = response['results']['name']
            else:
                raise RuntimeError('Access Denied.')

        self.__descriptor = self.__request_descriptor()

        return self

    def __assert_connection_to_xdmod_host(self):
        try:
            self.__request()
        except RuntimeError as e:
            raise RuntimeError('Could not connect to xdmod_host \''
                               + self.__xdmod_host + '\': ' + str(e)) from None

    def __request_json(self, path, post_fields, headers=None,
                       content_type=None):
        response = self.__request(path, post_fields, headers, content_type)
        return json.loads(response)

    def __request(self, path='', post_fields={}, headers=None,
                  content_type=None):
        self.__assert_runtime_context()
        self.__crl.reset()

        url = self.__xdmod_host + path
        self.__crl.setopt(pycurl.URL, url)

        if content_type == 'JSON':
            pf = post_fields
        else:
            pf = urlencode(post_fields)
        if pf:
            self.__crl.setopt(pycurl.POSTFIELDS, pf)

        if headers is None:
            headers = self.__headers
        self.__crl.setopt(pycurl.HTTPHEADER, headers)

        buffer = io.BytesIO()
        self.__crl.setopt(pycurl.WRITEDATA, buffer)

        try:
            self.__crl.perform()
        except pycurl.error as e:
            code, msg = e.args
            if code == pycurl.E_URL_MALFORMAT:
                msg = 'Malformed URL.'
            raise RuntimeError(msg) from None

        response = buffer.getvalue().decode()
        code = self.__crl.getinfo(pycurl.RESPONSE_CODE)
        if code != 200:
            msg = ''
            try:
                response_json = json.loads(response)
                msg = ': ' + response_json['message']
            except json.JSONDecodeError:
                pass
            raise RuntimeError('Error ' + str(code) + msg) from None
        return response

    def __assert_runtime_context(self):
        if not self.__in_runtime_context:
            raise RuntimeError('Method is being called outside of the runtime'
                               + ' context. Make sure this method is only'
                               + ' called within the body of a `with`'
                               + ' statement.')

    def __request_descriptor(self):
        response = self.__request_json('/controllers/metric_explorer.php',
                                       {'operation': 'get_dw_descripter'})

        if response['totalCount'] != 1:
            raise RuntimeError('Descriptor received with unexpected'
                               + ' structure.')

        return self.__deserialize_descriptor(response['data'][0]['realms'])

    def __deserialize_descriptor(self, serialized_descriptor):
        result = {}
        for realm in serialized_descriptor:
            result[realm] = {}
            for field in ('metrics', 'dimensions'):
                field_descriptor = serialized_descriptor[realm][field]
                result[realm][field] = [(id,
                                         field_descriptor[id]['text'],
                                         field_descriptor[id]['info'])
                                        for id in field_descriptor]
        return result

    def get_realms(self):
        """Get a tuple containing the valid realms in the data warehouse.

           Returns
           -------
           Tuple of str
               The valid realms.

           Raises
           ------
           RuntimeError
               If this method is called outside the runtime context.
        """
        self.__assert_runtime_context()
        return tuple(self.__descriptor)

    def get_metrics(self, realm):
        """Get a DataFrame containing the valid metrics for the given realm.

           Parameters
           ----------
           realm : str
               A realm in the data warehouse.

           Returns
           -------
           pandas.core.frame.DataFrame
               A Pandas DataFrame containing the ID, label, and description
               of each metric.

           Raises
           ------
           KeyError
               If `realm` is not one of the values from `get_realms()`.
           TypeError
               If `realm` is not a string.
           RuntimeError
               If this method is called outside the runtime context.
        """
        self.__assert_runtime_context()
        self.__validate_realm(realm)
        return self.__get_descriptor_data_frame(realm, 'metrics')

    def __validate_realm(self, realm):
        self.__assert_str('realm', realm)
        self.__assert_str_in_sequence(realm, self.get_realms(), 'realms',
                                      'Invalid realm \'' + realm + '\'')

    def __assert_str_in_sequence(self, string, sequence, label, msg_prologue):
        if string not in sequence:
            raise KeyError(msg_prologue + '. Valid ' + label + ' are: \''
                           + '\', \''.join(sequence)
                           + '\'.') from None

    def __get_descriptor_data_frame(self, realm, field):
        return self.__get_indexed_data_frame(data=self.__descriptor[realm]
                                                                   [field],
                                             columns=('id',
                                                      'label',
                                                      'description'),
                                             index='id')

    def __get_indexed_data_frame(self, data, columns, index):
        df = pd.DataFrame(data=data, columns=columns)
        df = df.set_index('id')
        return df

    def get_dimensions(self, realm):
        """Get a DataFrame containing the valid dimensions for the given realm.

           Parameters
           ----------
           realm : str
               A realm in the data warehouse.

           Returns
           -------
           pandas.core.frame.DataFrame
               A Pandas DataFrame containing the ID, label, and description
               of each dimension.

           Raises
           ------
           KeyError
               If `realm` is not one of the values from `get_realms()`.
           TypeError
               If `realm` is not a string.
           RuntimeError
               If this method is called outside the runtime context.
        """
        self.__assert_runtime_context()
        self.__validate_realm(realm)
        return self.__get_descriptor_data_frame(realm, 'dimensions')

    def get_filters(self, realm, dimension):
        """Get a DataFrame containing the valid filters for the given dimension
           of the given realm.

           Parameters
           ----------
           realm : str
               A realm in the data warehouse.
           dimension : str
               A dimension of the given realm in the data warehouse.

           Returns
           -------
           pandas.core.frame.DataFrame
               A Pandas DataFrame containing the ID and label of each filter.

           Raises
           ------
           KeyError
               If `realm` is not one of the values from `get_realms()` or
               `dimension` is not one of the IDs or labels from
               `get_dimensions()`
           TypeError
               If `realm` or `dimension` are not strings.
           RuntimeError
               If this method is called outside the runtime context.
        """
        self.__assert_runtime_context()
        self.__validate_realm(realm)
        self.__assert_str('dimension', dimension)

        dimension_id = self.__find_id_in_descriptor(realm,
                                                    'dimensions',
                                                    dimension)

        path = '/controllers/metric_explorer.php'

        post_fields = {'operation': 'get_dimension',
                       'dimension_id': dimension_id,
                       'realm': realm}

        response = self.__request_json(path, post_fields)
        data = [(datum['id'], datum['name']) for datum in response['data']]
        df = self.__get_indexed_data_frame(data=data,
                                           columns=('id', 'label'),
                                           index='id')
        return df

    def get_valid_values(self, parameter):
        """Get a collection of valid values for a given parameter.

           Parameters
           ----------
           parameter : str
               The name of the parameter.

           Returns
           -------
           tuple of str
               The collection of valid values.

           Raises
           ------
           KeyError
               If the given parameter does not have a collection of valid
               values.
        """
        if parameter not in self.__valid_values:
            raise KeyError('Parameter \'' + parameter
                           + '\' does not have a list of valid values.')

        return self.__valid_values[parameter]

    def __find_id_in_descriptor(self, realm, field, search_str):
        for (id_, text, info) in self.__descriptor[realm][field]:
            if id_ == search_str or text == search_str:
                return id_

        raise KeyError('\'' + search_str + '\' not found in ' + field
                       + ' of \'' + realm + '\' realm.')

    def get_aggregate_data(self,
                           duration='Previous month',
                           realm='Jobs',
                           metric='CPU Hours: Total',
                           dimension='None',
                           filters={},
                           timeseries=True,
                           aggregation_unit='Auto'):
        """Get a DataFrame containing aggregate data from the warehouse.

           Parameters
           ----------
           duration : str or object of length 2 of str, optional
               ...
           realm : str, optional
               ...
           metric : str, optional
               ...
           dimension : str, optional
               ...
           filters : dict of str, optional
               ...
           timeseries : bool, optional
               ...
           aggregation_unit : str, optional
               ...

           Returns
           -------
           pandas.core.frame.DataFrame
               A Pandas DataFrame containing the data...

           Raises
           ------
           KeyError
               If any of the parameters have invalid values. Valid realms
               come from `get_realms()`; valid metrics come from
               `get_metrics()`; valid dimensions and filters come from
               `get_dimensions()`; and valid durations and aggregation units
               come from `get_valid_values()`.
           RuntimeError
               If this method is called outside the runtime context or if
               there is an error requesting data from the warehouse.
           TypeError
               If any of the arguments are of the wrong type.
           ValueError
               If `duration` is an object but not of length 2.
        """
        self.__assert_runtime_context()

        (start, end) = self.__get_start_end_from_duration(duration)

        self.__validate_realm(realm)

        self.__assert_str('metric', metric)
        metric_id = self.__find_id_in_descriptor(realm,
                                                 'metrics',
                                                 metric)

        self.__assert_str('dimension', dimension)
        dimension_id = self.__find_id_in_descriptor(realm,
                                                    'dimensions',
                                                    dimension)

        self.__assert_dict_of_str('filters', filters)
        self.__assert_bool('timeseries', timeseries)
        self.__validate_str('aggregation_unit', aggregation_unit)

        post_fields = {'operation': 'get_data',
                       'start_date': start,
                       'end_date': end,
                       'realm': realm,
                       'statistic': metric_id,
                       'group_by': dimension_id,
                       'dataset_type': 'timeseries' if timeseries
                                       else 'aggregate',
                       'aggregation_unit': aggregation_unit,
                       'public_user': 'true',
                       'timeframe_label': '2016',
                       'scale': '1',
                       'thumbnail': 'n',
                       'query_group': 'po_usage',
                       'display_type': 'line',
                       'combine_type': 'side',
                       'limit': '10',
                       'offset': '0',
                       'log_scale': 'n',
                       'show_guide_lines': 'y',
                       'show_trend_line': 'y',
                       'show_percent_alloc': 'n',
                       'show_error_bars': 'y',
                       'show_aggregate_labels': 'n',
                       'show_error_labels': 'n',
                       'show_title': 'y',
                       'width': '916',
                       'height': '484',
                       'legend_type': 'bottom_center',
                       'font_size': '3',
                       'inline': 'n',
                       'format': 'csv'}

        for dimension in filters:
            dimension_id = self.__find_id_in_descriptor(realm,
                                                        'dimensions',
                                                        dimension)
            valid_filters = self.get_filters(realm, dimension_id)
            filter_values = []
            for filter_ in filters[dimension]:
                self.__assert_str('filter value', filter_)
                if filter_ in valid_filters.index:
                    filter_value = filter_
                elif filter_ in valid_filters['label'].values:
                    filter_value = valid_filters.index[valid_filters['label']
                                                       == filter_].tolist()[0]
                else:
                    raise KeyError('Filter value `' + filter_
                                   + '` not found in `' + dimension
                                   + '` dimension of `' + realm
                                   + '` realm.')
                filter_values.append(filter_value)
            post_fields[dimension_id + '_filter'] = ','.join(filter_values)

        response = self.__get_usage_data(post_fields)

        csvdata = csv.reader(response.splitlines())

        if not timeseries:
            return self.__xdmod_csv_to_pandas(csvdata)
        else:
            labelre = re.compile(r'\[([^\]]+)\].*')
            timestamps = []
            data = []
            for line_num, line in enumerate(csvdata):
                if line_num == 5:
                    start, end = line
                elif line_num == 7:
                    dimensions = []
                    for label in line[1:]:
                        match = labelre.match(label)
                        if match:
                            dimensions.append(html.unescape(match.group(1)))
                        else:
                            dimensions.append(html.unescape(label))
                elif line_num > 7 and len(line) > 1:
                    date_string = line[0]
                    # Match YYYY-MM-DD
                    if re.match(r'^[0-9]{4}-[0-9]{2}-[0-9]{2}$', line[0]):
                        format = '%Y-%m-%d'
                    # Match YYYY-MM
                    elif re.match(r'^[0-9]{4}-[0-9]{2}$', line[0]):
                        format = '%Y-%m'
                    # Match YYYY
                    elif re.match(r'^[0-9]{4}$', line[0]):
                        format = '%Y'
                    # Match YYYY Q#
                    elif re.match(r'^[0-9]{4} Q[0-9]$', line[0]):
                        year, quarter = line[0].split(' ')
                        if quarter == 'Q1':
                            month = '01'
                        elif quarter == 'Q2':
                            month = '04'
                        elif quarter == 'Q3':
                            month = '07'
                        elif quarter == 'Q4':
                            month = '10'
                        else:
                            raise Exception('Unsupported date quarter'
                                            + ' specification ' + line[0]
                                            + '.')
                        date_string = year + '-' + month + '-01'
                        format = '%Y-%m-%d'
                    else:
                        raise Exception('Unsupported date specification '
                                        + line[0] + '.')
                    timestamps.append(datetime.strptime(date_string, format))
                    data.append(numpy.asarray(line[1:], dtype=numpy.float64))

            return pd.DataFrame(data=data,
                                index=pd.Series(data=timestamps, name='Time'),
                                columns=dimensions)

    def __get_start_end_from_duration(self, duration):
        if isinstance(duration, str):
            self.__validate_str('duration', duration)
            (start, end) = self.__DURATION_TO_START_END[duration]
        else:
            try:
                (start, end) = duration
            except (TypeError, ValueError) as error:
                raise type(error)('`duration` must be a string'
                                  + ' or an object with 2 items.') from None
        return (start, end)

    def __validate_str(self, key, value):
        self.__assert_str(key, value)
        self.__assert_str_in_sequence(value, self.__valid_values[key],
                                      'values', 'Invalid value for `' + key
                                                + '`: \'' + value + '\'')

    def __assert_dict_of_str(self, name, obj):
        type_error_msg = '`' + name + '` must be a dictionary of strings.'

        if not isinstance(obj, Mapping):
            raise TypeError(type_error_msg)

        for key in obj:
            if not isinstance(obj[key], str):
                raise TypeError(type_error_msg)

    def __assert_bool(self, name, obj):
        if not isinstance(obj, bool):
            raise TypeError('`' + name + '` must be a Boolean.')

    def __get_usage_data(self, post_fields):
        response = self.__request('/controllers/user_interface.php',
                                  post_fields)

        return response

    def __xdmod_csv_to_pandas(self, rd):
        groups = []
        data = []
        for line_num, line in enumerate(rd):
            if line_num == 5:
                start, end = line
            elif line_num == 7:
                group, metric = line
            elif line_num > 7 and len(line) > 1:
                groups.append(html.unescape(line[0]))
                data.append(numpy.float64(line[1]))

        if len(data) == 0:
            return pd.Series(dtype='float64')

        return pd.Series(data=data, index=groups, name=metric)

    def get_raw_data(self, realm, start, end, filters, stats):
        post_fields = json.dumps({'realm': realm,
                                  'start_date': start,
                                  'end_date': end,
                                  'params': filters,
                                  'stats': stats})

        headers = self.__headers + ['Accept: application/json',
                                    'Content-Type: application/json',
                                    'charset: utf-8']

        result = self.__request_json(path='/rest/v1/warehouse/rawdata',
                                     post_fields=post_fields,
                                     headers=headers,
                                     content_type='JSON')

        return pd.DataFrame(data=result['data'],
                            columns=result['stats'],
                            dtype=numpy.float64)

    def whoami(self):
        if self.__username:
            return self.__username
        return 'Not logged in'

    def compliance(self, timeframe):
        response = self.__request_json('/controllers/compliance.php',
                                       {'timeframe_mode': timeframe})

        return response

    def resources(self):
        names = []
        types = []
        resource_ids = []

        cdata = self.compliance('to_date')
        for resource in cdata['metaData']['fields']:
            if resource['name'] == 'requirement':
                continue
            names.append(resource['header'][:-7].split('>')[1].replace('-',
                                                                       ' '))
            types.append(resource['status'].split('|')[0].strip())
            resource_ids.append(resource['resource_id'])

        return pd.Series(data=types, index=names)

    def get_qualitydata(self, params, is_numpy=False):
        type_to_title = {
            'gpu': '% of jobs with GPU information',
            'hardware': '% of jobs with hardware perf information',
            'cpu': '% of jobs with cpu usage information',
            'script': '% of jobs with Job Batch Script information',
            'realms': '% of jobs in the SUPReMM realm compared to Jobs realm'}

        response = self.__request_json('/rest/supremm_dataflow/quality',
                                       params)

        if response['success']:
            result = response['result']
            jobs = [job for job in result]
            dates = [date.strftime('%Y-%m-%d') for date
                     in pd.date_range(params['start'],
                                      params['end'],
                                      freq='D').date]

            quality = numpy.empty((len(jobs), len(dates)))

            for i in range(len(jobs)):
                for j in range(len(dates)):
                    job_i = result[jobs[i]]
                    if job_i.get(dates[j], numpy.nan) != 'N/A':
                        quality[i, j] = job_i.get(dates[j], numpy.nan)
                    else:
                        quality[i, j] = numpy.nan
            if is_numpy:
                return quality
            df = pd.DataFrame(data=quality, index=jobs, columns=dates)
            df.name = type_to_title[params['type']]
            return df

    def __exit__(self, tpe, value, tb):
        if self.__cookie_file:
            os.unlink(self.__cookie_file)
        if self.__crl:
            self.__crl.close()
        self.__username = None
        self.__in_runtime_context = False
