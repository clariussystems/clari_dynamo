# -*- coding: utf-8 -*-
from __future__ import absolute_import, division, print_function, unicode_literals
from builtins import (bytes, str, open, super, range, zip, round, input, int, pow, object)
from clari_dynamo.conf.constants import *

import random
import time
from datetime import datetime
import sys
import traceback

# Hack for KMS patch - TODO: Remove after https://github.com/boto/boto/issues/2921
sys.path.insert(0, BOTO_PATH)

from boto.dynamodb2.exceptions import ProvisionedThroughputExceededException
from boto.dynamodb2.exceptions import ConditionalCheckFailedException
from boto.dynamodb2.layer1 import DynamoDBConnection
from boto.dynamodb2.table import Table as BotoTable
from boto.dynamodb2.items import Item as BotoItem
from boto.dynamodb.types import Binary

from clari_dynamo import s3_kms
from clari_dynamo.local_dynamo.localdb import LocalDb
from clari_dynamo.conf.cd_logger import logging
from clari_dynamo.instrumentation import item_op, table_op

MAX_RETRY_COUNT = 4


class ClariDynamo(object):
    def __init__(self, aws_access_key, aws_secret_access_key, is_secure,
                 is_remote=False, host=None, port=None, in_memory=False,
                 auth_func=None):
        if auth_func and not auth_func():
            raise self.AuthException()

        self.host = host
        self.port = port
        self.is_secure = is_secure
        self.is_remote = is_remote
        self.in_memory = in_memory
        kwargs = {
            'aws_access_key_id':     aws_access_key,
            'aws_secret_access_key': aws_secret_access_key,
            'is_secure':             is_secure
        }
        if not is_remote:
            kwargs['host'] = host
            kwargs['port'] = port
            self.local_db = LocalDb(port, in_memory)

        self.connection = self._create_dynamo_connection(aws_access_key,
                                               aws_secret_access_key, host,
                                               is_secure, port)

    @item_op
    def query(self, table_name, purpose, tenant_id, **query):
        boto_table = self.get_table(table_name)
        # TODO: Paging -
        # Implement paging by serializing underlying page data and
        # storing it for subsequent request.
        return boto_table.query_2(**query)

    @item_op
    def get_item(self, table_name, tenant_id, purpose, **id_query):
        boto_table = self.get_table(table_name)
        item = self._get_with_retries(boto_table, table_name, id_query, retry=0)
        self._check_tenant_id(item, tenant_id)
        self._check_for_meta(item._data, boto_table, operation='get')
        return item

    @item_op
    def put_item(self, table_name, item, tenant_id, purpose, overwrite=False,
                 condition=None, vars=None):
        """
        Puts item into DynamoDB
        :param table_name:
        :param item:
        :param tenant_id: i.e. a user / customer id used for maintaining
                          data access boundaries between db tenants
        :param purpose:
        :param condition: DynamoDB condition # https://goo.gl/VRx8ST
        :return:
        """
        boto_table = self.get_table(table_name)
        assert type(item) == dict
        assert isinstance(tenant_id, str)
        item['tenant_id'] = tenant_id
        item['encrypted_tenant_id'] = CRYPTO.encrypt(bytes(tenant_id, 'UTF-8'))
        item['created_at'] = item['updated_at'] = (
            str(datetime.now()))
        self._check_for_meta(item, boto_table, operation='put')
        return self._put_with_retries(boto_table,
                    self._get_table_name(table_name), item, overwrite,
                    condition, vars, retry=0)

    @item_op
    def delete_item(self, table_name, item, tenant_id, purpose):
        boto_table = self.get_table(table_name)
        data = item._data
        assert type(data) == dict
        self._check_for_meta(data, boto_table, operation='delete')
        item.delete()

    @table_op
    def create_table(self, table_name, **kwargs):
        return BotoTable.create(self._get_table_name(table_name),
                                connection=self.connection, **kwargs)

    @table_op
    def drop_table(self, table_name):
        return self.connection.delete_table(self._get_table_name(table_name))

    @table_op
    def get_table(self, table_name, **kwargs):
        ret = BotoTable(self._get_table_name(table_name),
                connection=self.connection, **kwargs)
        ret.clari_description = ret.describe() # Arg, props not correct unless you call this
        return ret

    @table_op
    def _change_throughput(self, new_throughput, boto_table, table_name):
        try:
            logging.warn('Attempting to increase throughput of ' + table_name)
            self.connection.update_table(table_name,
                                   provisioned_throughput=new_throughput)
        except Exception as e:
            # TODO: Fail gracefully here on Validation Exception.
            # TODO: Don't refresh table info after getting throughput exceeded
            exc_info = sys.exc_info()
            logging.error('Could not increase table throughput will continue '
                          'retrying. Error was: %s %s %s',
                          exc_info[0], exc_info[1], exc_info[2])
        else:
            logging.info('Successfully increased throughput of ' + table_name)

    @table_op
    def list_tables(self):
        table_names = self.connection.list_tables()['TableNames']
        table_data = {}
        for table_name in table_names:
            table_data[table_name] = self.connection.describe_table(table_name)['Table']
        return table_data

    @table_op
    def has_table(self, table_name):
        full_table_name = self._get_table_name(table_name)
        return full_table_name in self.list_tables()

    def _get_table_name(self, name):
        return 'clari_dynamo_' + ENV_NAME + '_' + name

    def _stop_local(self):
        if not self.is_remote:
            self.local_db.stop()

    def _check_tenant_id(self, item, tenant_id):
        assert item['tenant_id'] == tenant_id
        assert item['tenant_id'] == CRYPTO.decrypt(bytes(
            item['encrypted_tenant_id'], 'UTF-8'))

    def _handle_s3_backed_item(self, table, operation, parent, key, value):
        if operation == 'get':
            parent[key] = s3_kms.get(value["$s3_key"])
        elif operation == 'put':
            s3_key = s3_kms.put(table.table_name, key, value['$data'])
            value['$s3_key'] = s3_key.key
            del value['$data']
        elif operation == 'delete':
            s3_kms.delete(value["$s3_key"])

    def _handle_base64_item(self, operation, parent, key, value):
        if operation == 'get':
            pass
        elif operation == 'put':
            binary_data = Binary('')

            #  base64 comes in from API, so set directly (minor hack)
            binary_data.value = value['$data']

            assert len(value) == 2, \
                'only $data and $base64 should be set on binary item'

            parent[key] = binary_data

        elif operation == 'delete':
            pass

    def _check_for_meta(self, item, boto_table, operation):
        for key, value in item.iteritems():
            if type(value) == dict:
                # Read meta info
                if value.get("$s3"):
                    self._handle_s3_backed_item(boto_table, operation, item,
                                                key, value)
                if value.get('$base64'):
                    self._handle_base64_item(operation, item, key, value)
                if value.get('$data'):
                    item[key] = value.get('$data')
            if type(value) in (dict, list):
                self._check_for_meta(value, boto_table, operation)

    def _put_with_retries(self, boto_table, table_name, data, overwrite,
                          condition, vars, retry):
        boto_item = BotoItem(boto_table, data)

        # Use internal boto method to access to full AWS Dynamo capabilities
        final_data = boto_item.prepare_full()

        def try_function():
            expected = boto_item.build_expects() if overwrite is False else None
            boto_table.connection.put_item(table_name, final_data,
                expected=expected, # Don't overwrite
                condition_expression=condition,
                expression_attribute_values=vars)
        try:
            ret = self._attempt_throttled_operation(try_function, retry,
                                                    boto_table,
                    increased_throughput=get_double_writes(boto_table))
        except ConditionalCheckFailedException as e:
            raise self.ClariDynamoConditionCheckFailedException(str(e) + ' - ' +
                'This could be due to a duplicate insertion.')
        return ret

    def _get_with_retries(self, boto_table, table_name, id_query, retry):
        try_function = lambda: (boto_table.get_item(**id_query))
        ret = self._attempt_throttled_operation(try_function,
                retry, boto_table,
                increased_throughput=get_double_reads(boto_table))
        return ret

    def _attempt_throttled_operation(self, try_function,
            retry_number, boto_table, increased_throughput):
        try:
            ret = try_function()
        except ProvisionedThroughputExceededException as e:
            if RETRY_ON_THROUGHPUT_EXCEEDED and retry_number < MAX_RETRY_COUNT:
                self._handle_throughput_exceeded(increased_throughput,
                                                 retry_number, boto_table)
                ret = self._attempt_throttled_operation(try_function,
                            retry_number + 1, boto_table, increased_throughput)
            else:
                exc_info = sys.exc_info()
                raise exc_info[0], exc_info[1], exc_info[2]
        return ret

    def _get_secs_since_increase(self, boto_table):
        last_modified = datetime.fromtimestamp(boto_table.clari_description['Table']
            ['ProvisionedThroughput']['LastIncreaseDateTime'])
        secs_since_increase = (datetime.now() - last_modified).total_seconds()
        return secs_since_increase

    def _handle_throughput_exceeded(self, new_throughput, retry, boto_table):
        logging.warn(
            'ProvisionedThroughputExceededException retrying: ' +
            str(retry))
        if retry == 0:
            # Only increase throughput on first retry for this request.
            # assert False
            # TODO: Create our own last_increase_time in meta
            # TODO: See if throughput increased.
            secs_since_increase = self._get_secs_since_increase(boto_table)
            if secs_since_increase > 5:
                self._change_throughput(new_throughput, boto_table,
                                        boto_table.table_name)

        self._exponential_splay(retry)

    def _exponential_splay(self, retry):
        if IS_TEST:
            sleep_coeff = 0
        else:
            sleep_coeff = 1

        # random => ! herd
        # Max: 2 ** 4 = 16 seconds
        time_to_sleep = sleep_coeff * 2 ** retry * random.random()
        logging.info('sleeping for %f seconds' % time_to_sleep)
        time.sleep(time_to_sleep)

    def _create_dynamo_connection(self, aws_access_key, aws_secret_access_key, host,
                              is_secure, port):
        return DynamoDBConnection(
            aws_access_key_id=aws_access_key,
            aws_secret_access_key=aws_secret_access_key,
            host=host,
            port=port,
            is_secure=is_secure)

    class AuthException(Exception):
        pass

    class ClariDynamoConditionCheckFailedException(Exception):
        pass


READ_KEY  = 'ReadCapacityUnits'.encode('ascii')
WRITE_KEY = 'WriteCapacityUnits'.encode('ascii')


def get_double_writes(boto_table):
    throughput = boto_table.throughput
    return {READ_KEY:  throughput['read'],
            WRITE_KEY: throughput['write'] * 2}


def get_double_reads(boto_table):
    throughput = boto_table.throughput
    return {READ_KEY:  throughput['read'] * 2,
            WRITE_KEY: throughput['write']}