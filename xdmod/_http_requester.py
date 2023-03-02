import xdmod._validator as _validator
import io
import json
import os
import pycurl
import tempfile
from urllib.parse import urlencode


class _HttpRequester:
    def __init__(self, xdmod_host, api_token):
        self.__in_runtime_context = False
        _validator._assert_str('xdmod_host', xdmod_host)
        self.__xdmod_host = xdmod_host
        if api_token:
            _validator._assert_str('api_token', api_token)
        self.__api_token = api_token
        self.__crl = None
        self.__cookie_file = None
        self.__headers = []
        self.__init_api_token()

    def _start_up(self):
        self.__in_runtime_context = True
        self.__crl = pycurl.Curl()
        self._assert_connection_to_xdmod_host()
        if self.__api_token:
            _, self.__cookie_file = tempfile.mkstemp()
            self.__crl.setopt(pycurl.COOKIEJAR, self.__cookie_file)
            self.__crl.setopt(pycurl.COOKIEFILE, self.__cookie_file)
            response = self._request_json(
                '/rest/auth/login', self.__api_token
            )
            if response['success'] is True:
                token = response['results']['token']
                self.__headers = ['Token: ' + token]
                self.__crl.setopt(pycurl.HTTPHEADER, self.__headers)
                self.__username = response['results']['name']
            else:
                raise RuntimeError('Access Denied.')

    def _tear_down(self):
        if self.__cookie_file:
            os.unlink(self.__cookie_file)
        if self.__crl:
            self.__crl.close()
        self.__in_runtime_context = False

    def _assert_connection_to_xdmod_host(self):
        try:
            self._request()
        except RuntimeError as e:
            raise RuntimeError(
                'Could not connect to xdmod_host \'' + self.__xdmod_host
                + '\': ' + str(e)
            ) from None

    def _request_json(
            self, path, post_fields=None, headers=None, content_type=None):
        response = self._request(path, post_fields, headers, content_type)
        return json.loads(response)

    def _request(
            self, path='', post_fields=None, headers=None, content_type=None):
        _validator._assert_runtime_context(self.__in_runtime_context)
        self.__crl.reset()
        url = self.__xdmod_host + path
        self.__crl.setopt(pycurl.URL, url)
        if post_fields:
            if content_type == 'JSON':
                pf = post_fields
            else:
                pf = urlencode(post_fields)
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

    def __init_api_token(self):
        if not self.__api_token:
            username = self.__get_environment_variable('XDMOD_USER')
            password = self.__get_environment_variable('XDMOD_PASS')
            self.__api_token = {
                'username': username,
                'password': password
            }

    def __get_environment_variable(self, name):
        try:
            return os.environ[name]
        except KeyError:
            raise KeyError(
                name + ' environment variable has not been set.'
            ) from None
