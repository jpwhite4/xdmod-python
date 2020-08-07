import numpy
import csv
import pycurl
from urllib.parse import urlencode
import pandas as pd
import io
import tempfile
import json
import os


class DataWareHouse:
    """ Access the XDMoD datawarehouse via XDMoD's network API """

    def __init__(self, xdmodhost, apikey=None):
        self.xdmodhost = xdmodhost
        self.apikey = apikey
        self.crl = None
        self.cookiefile = None
        self.descriptor = None

        if not self.apikey:
            try:
                self.apikey = {
                    'username': os.environ['XDMOD_USER'],
                    'password': os.environ['XDMOD_PASS']
                }
            except KeyError:
                pass

    def __enter__(self):
        self.crl = pycurl.Curl()
        if self.apikey:
            _, self.cookiefile = tempfile.mkstemp()
            self.crl.setopt(pycurl.COOKIEJAR, self.cookiefile)
            self.crl.setopt(pycurl.COOKIEFILE, self.cookiefile)

            self.crl.setopt(pycurl.URL, self.xdmodhost + '/rest/auth/login')
            pf = urlencode(self.apikey)
            b_obj = io.BytesIO()
            self.crl.setopt(pycurl.WRITEDATA, b_obj)
            self.crl.setopt(pycurl.POSTFIELDS, pf)
            self.crl.perform()

            response = json.loads(b_obj.getvalue().decode('utf8'))
            if response['success'] is True:
                token = response['results']['token']
                self.crl.setopt(pycurl.HTTPHEADER, ['Token: ' + token])
            else:
                raise RuntimeError('Access Denied')

            print("logged in as " + response['results']['name'])

        return self

    def __exit__(self, tpe, value, tb):
        if self.cookiefile:
            os.unlink(self.cookiefile)
        if self.crl:
            self.crl.close()

    def realms(self):
        info = self.get_descriptor()
        return [*info['realms']]

    def metrics(self, realm):
        info = self.get_descriptor()
        output = []
        for metric, minfo in info['realms'][realm]['metrics'].items():
            output.append((metric, minfo['text'] + ': ' + minfo['info']))
        return output

    def dimensions(self, realm):
        info = self.get_descriptor()
        output = []
        for dimension, dinfo in info['realms'][realm]['dimensions'].items():
            output.append((dimension, dinfo['text'] + ': ' + dinfo['info']))
        return output

    def get_descriptor(self):
        if self.descriptor:
            return self.descriptor

        self.crl.setopt(pycurl.URL,
                        self.xdmodhost + '/controllers/metric_explorer.php')
        config = {'operation': 'get_dw_descripter'}
        pf = urlencode(config)
        b_obj = io.BytesIO()
        self.crl.setopt(pycurl.WRITEDATA, b_obj)
        self.crl.setopt(pycurl.POSTFIELDS, pf)
        self.crl.perform()

        get_body = b_obj.getvalue()

        response = json.loads(get_body.decode('utf8'))

        if response['totalCount'] != 1:
            raise RuntimeError('Retrieving XDMoD data descriptor')

        self.descriptor = response['data'][0]

        return self.descriptor

    def aggregate(self, realm, group_by, statistic, start, end):

        config = {
            'start_date': start,
            'end_date': end,
            'realm': realm,
            'statistic': statistic,
            'group_by': group_by,
            'public_user': 'true',
            'timeframe_label': '2016',
            'scale': '1',
            'aggregation_unit': 'Auto',
            'dataset_type': 'aggregate',
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
            'operation': 'get_data',
            'format': 'csv'
        }

        response = self.get_usagedata(config)
        csvdata = csv.reader(response.splitlines())

        return self.xdmodcsvtopandas(csvdata)

    def get_usagedata(self, config):

        self.crl.setopt(pycurl.URL,
                        self.xdmodhost + '/controllers/user_interface.php')
        pf = urlencode(config)
        b_obj = io.BytesIO()
        self.crl.setopt(pycurl.WRITEDATA, b_obj)
        self.crl.setopt(pycurl.POSTFIELDS, pf)
        self.crl.perform()

        get_body = b_obj.getvalue()

        return get_body.decode('utf8')

    def xdmodcsvtopandas(self, rd):
        groups = []
        data = []
        for line_num, line in enumerate(rd):
            if line_num == 1:
                title = line[0]
            elif line_num == 5:
                start, end = line
            elif line_num == 7:
                group, metric = line
            elif line_num > 7 and len(line) > 1:
                groups.append(line[0])
                data.append(numpy.float64(line[1]))

        return pd.DataFrame(data=data, index=groups)
